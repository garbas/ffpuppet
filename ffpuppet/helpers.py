# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import logging
import os
import platform
import re
import shutil
import stat
import tempfile
import time

from xml.etree import ElementTree
import psutil

log = logging.getLogger("ffpuppet")  # pylint: disable=invalid-name

__author__ = "Tyson Smith"
__all__ = ("check_prefs", "create_profile", "onerror", "prepare_environment", "wait_on_files")


class SanitizerConfig(object):
    re_delim = re.compile(r":(?![\\|/])")

    def __init__(self):
        self._options = dict()

    def add(self, key, value, overwrite=False):
        if key not in self._options or overwrite:
            self._options[key] = value

    def load_options(self, env, key):
        assert isinstance(env, dict)
        if key not in env:
            return None
        assert isinstance(env[key], str)
        assert " " not in env[key], "%s should not contain spaces, join options with ':'" % key
        for option in self.re_delim.split(env[key]):
            try:
                opt_name, opt_value = option.split("=")
                # add a sanity check for suppression files
                if opt_name == "suppressions":
                    opt_value = os.path.abspath(os.path.expanduser(opt_value))
                    if not os.path.isfile(opt_value):
                        raise IOError("Suppressions file %r does not exist" % opt_value)
                self._options[opt_name] = opt_value
            except ValueError:
                log.warning("Malformed option in %r", key)

    @property
    def options(self):
        return ":".join(["=".join([k, v]) for k, v in self._options.items()])


def check_prefs(prof_prefs, input_prefs):
    """
    Check that the current prefs.js file in use by the browser contains all the requested prefs.

    NOTE: There will be false positives if input_prefs does not adhere to the formatting that
    is used in prefs.js file generated by the browser.

    @type prof_prefs: String
    @param prof_prefs: Path to profile prefs.js file

    @type input_prefs: String
    @param input_prefs: Path to prefs.js file that contains prefs that should be merged
                        into the prefs.js file generated by the browser

    @rtype: bool
    @return: True if all prefs in input_prefs are merged otherwise False
    """

    if not os.path.isfile(input_prefs):
        raise IOError("Cannot find %r" % input_prefs)
    if not os.path.isfile(prof_prefs):
        raise IOError("Cannot find %r" % prof_prefs)

    with open(prof_prefs, "r") as p_fp, open(input_prefs, "r") as i_fp:
        p_prefs = {pref.split(",")[0] for pref in p_fp if pref.startswith("user_pref(")}
        i_prefs = {pref.split(",")[0] for pref in i_fp if pref.startswith("user_pref(")}

    missing_prefs = i_prefs - p_prefs
    log.debug(
        "prefs not set %r",
        ", ".join([m_pref.lstrip("user_pref(") for m_pref in missing_prefs]))

    return not bool(missing_prefs)


def configure_sanitizers(env, target_dir, log_path):
    # setup Address Sanitizer options if not set manually
    # https://github.com/google/sanitizers/wiki/AddressSanitizerFlags
    # https://github.com/google/sanitizers/wiki/SanitizerCommonFlags
    asan_config = SanitizerConfig()
    asan_config.load_options(env, "ASAN_OPTIONS")
    asan_config.add("abort_on_error", "true")
    #asan_config.add("alloc_dealloc_mismatch", "false")  # different defaults per OS
    asan_config.add("allocator_may_return_null", "true")
    asan_config.add("check_initialization_order", "true")
    #asan_config.add("detect_stack_use_after_return", "true")  # https://bugzil.la/1057551
    #asan_config.add("detect_stack_use_after_scope", "true")
    asan_config.add("detect_leaks", "false")
    asan_config.add("disable_coredump", "true")
    # log_path is required for logging to function properly
    asan_config.add("log_path", "'%s'" % log_path, overwrite=True)
    asan_config.add("sleep_before_dying", "0")
    asan_config.add("strict_init_order", "true")
    asan_config.add("symbolize", "true")
    env["ASAN_OPTIONS"] = asan_config.options

    # setup Leak Sanitizer options if not set manually
    # https://github.com/google/sanitizers/wiki/AddressSanitizerLeakSanitizer
    lsan_config = SanitizerConfig()
    lsan_config.load_options(env, "LSAN_OPTIONS")
    lsan_config.add("max_leaks", "1")
    lsan_config.add("print_suppressions", "false")
    env["LSAN_OPTIONS"] = lsan_config.options

    # setup Undefined Behavior Sanitizer options if not set manually
    ubsan_config = SanitizerConfig()
    ubsan_config.load_options(env, "UBSAN_OPTIONS")
    ubsan_config.add("log_path", "'%s'" % log_path, overwrite=True)
    ubsan_config.add("print_stacktrace", "1")
    env["UBSAN_OPTIONS"] = ubsan_config.options

    if "ASAN_SYMBOLIZER_PATH" not in env:
        # ASAN_SYMBOLIZER_PATH only needs to be set on platforms other than Windows
        if not platform.system().lower().startswith("windows"):
            symbolizer_bin = os.path.join(target_dir, "llvm-symbolizer")
            if os.path.isfile(symbolizer_bin):
                env["ASAN_SYMBOLIZER_PATH"] = symbolizer_bin
        elif not os.path.join(target_dir, "llvm-symbolizer.exe"):
            log.warning("llvm-symbolizer.exe should be next to the target binary")
    elif "ASAN_SYMBOLIZER_PATH" in env and not os.path.isfile(env["ASAN_SYMBOLIZER_PATH"]):
        log.warning("Invalid ASAN_SYMBOLIZER_PATH (%s)", env["ASAN_SYMBOLIZER_PATH"])


def create_profile(extension=None, prefs_js=None, template=None):
    """
    Create a profile to be used with Firefox

    @type extension: String, or list of Strings
    @param extension: Path to an extension (e.g. DOMFuzz fuzzPriv extension) to be installed.

    @type prefs_js: String
    @param prefs_js: Path to a prefs.js file to install in the Firefox profile.

    @type template: String
    @param template: Path to an existing profile directory to use.

    @rtype: String
    @return: Path to directory to be used as a profile
    """

    profile = tempfile.mkdtemp(prefix="ffprof_")
    log.debug("profile directory: %r", profile)

    if template is not None:
        log.debug("using profile template: %r", template)
        shutil.rmtree(profile) # reuse the directory name
        if not os.path.isdir(template):
            raise IOError("Cannot find template profile: %r" % template)
        shutil.copytree(template, profile)
        invalid_prefs = os.path.join(profile, "Invalidprefs.js")
        # if Invalidprefs.js was copied from the template profile remove it
        if os.path.isfile(invalid_prefs):
            os.remove(invalid_prefs)

    if prefs_js is not None:
        log.debug("using prefs.js: %r", prefs_js)
        if not os.path.isfile(prefs_js):
            shutil.rmtree(profile, True) # clean up on failure
            raise IOError("prefs.js file does not exist: %r" % prefs_js)
        shutil.copyfile(prefs_js, os.path.join(profile, "prefs.js"))

        # times.json only needs to be created when using a custom pref.js
        times_json = os.path.join(profile, "times.json")
        if not os.path.isfile(times_json):
            with open(times_json, "w") as times_fp:
                times_fp.write('{"created":%d}' % (int(time.time()) * 1000))

    # extension support
    try:
        if extension is None:
            extensions = []
        elif isinstance(extension, (list, tuple)):
            extensions = extension
        else:
            extensions = [extension]
        if extensions and not os.path.isdir(os.path.join(profile, "extensions")):
            os.mkdir(os.path.join(profile, "extensions"))
        for ext in extensions:
            if os.path.isfile(ext) and ext.endswith(".xpi"):
                shutil.copyfile(
                    ext,
                    os.path.join(profile, "extensions", os.path.basename(ext)))
            elif os.path.isdir(ext):
                # read manifest to see what the folder should be named
                ext_name = None
                if os.path.isfile(os.path.join(ext, "manifest.json")):
                    try:
                        with open(os.path.join(ext, "manifest.json")) as manifest:
                            manifest = json.load(manifest)
                        ext_name = manifest["applications"]["gecko"]["id"]
                    except (IOError, KeyError, ValueError) as exc:
                        log.debug("Failed to parse manifest.json: %s", exc)
                elif os.path.isfile(os.path.join(ext, "install.rdf")):
                    try:
                        xmlns = {"x": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
                                 "em": "http://www.mozilla.org/2004/em-rdf#"}
                        tree = ElementTree.parse(os.path.join(ext, "install.rdf"))
                        assert tree.getroot().tag == "{%s}RDF" % xmlns["x"]
                        ids = tree.findall("./x:Description/em:id", namespaces=xmlns)
                        assert len(ids) == 1
                        ext_name = ids[0].text
                    except (AssertionError, IOError, ElementTree.ParseError) as exc:
                        log.debug("Failed to parse install.rdf: %s", exc)
                if ext_name is None:
                    raise RuntimeError("Failed to find extension id in manifest: %r" % ext)
                shutil.copytree(
                    os.path.abspath(ext),
                    os.path.join(profile, "extensions", ext_name))
            else:
                raise RuntimeError("Unknown extension: %r" % ext)
    except:
        shutil.rmtree(profile, True) # cleanup on failure
        raise
    return profile


def onerror(func, path, _exc_info):
    """
    Error handler for `shutil.rmtree`.

    If the error is due to an access error (read only file)
    it attempts to add write permission and then retries.

    If the error is for another reason it re-raises the error.

    Copyright Michael Foord 2004
    Released subject to the BSD License
    ref: http://www.voidspace.org.uk/python/recipebook.shtml#utils

    Usage : `shutil.rmtree(path, onerror=onerror)`
    """
    if not os.access(path, os.W_OK):
        # Is the error an access error?
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        # this should only ever be called from an exception context
        raise  # pylint: disable=misplaced-bare-raise


def prepare_environment(target_dir, sanitizer_log, env_mod=None):
    """
    Get the string environment that is used when launching the browser.

    @type target_dir: String
    @param target_dir: Path to the directory containing the Firefox binary

    @type sanitizer_log: String
    @param sanitizer_log: Log prefix set with ASAN_OPTIONS=log_path=<sanitizer_log>

    @type env_mod: dict
    @param env_mod: Environment modifier. Add, remove and update entries in the prepared
                    environment via this dict. Add and update using key, value pairs where
                    value is a string and to remove set the value to None. If it is None no
                    extra modifications are made.

    @rtype: dict
    @return: A dict representing the string environment
    """
    env = dict(os.environ)

    # https://developer.gimp.org/api/2.0/glib/glib-running.html#G_SLICE
    env["G_SLICE"] = "always-malloc"
    env["MOZ_CC_RUN_DURING_SHUTDOWN"] = "1"
    env["MOZ_CRASHREPORTER"] = "1"
    env["MOZ_CRASHREPORTER_NO_REPORT"] = "1"
    env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
    env["MOZ_DISABLE_GMP_SANDBOX"] = "1"
    env["MOZ_DISABLE_GPU_SANDBOX"] = "1"
    env["MOZ_DISABLE_NPAPI_SANDBOX"] = "1"
    env["MOZ_GDB_SLEEP"] = "0"
    env["XRE_NO_WINDOWS_CRASH_DIALOG"] = "1"
    env["XPCOM_DEBUG_BREAK"] = "warn"
    # https://bugzilla.mozilla.org/show_bug.cgi?id=1305151
    # skia assertions are easily hit and mostly due to precision, disable them.
    if "MOZ_SKIA_DISABLE_ASSERTS" not in env:
        env["MOZ_SKIA_DISABLE_ASSERTS"] = "1"

    if "RUST_BACKTRACE" not in env:
        env["RUST_BACKTRACE"] = "full"

    configure_sanitizers(env, target_dir, sanitizer_log)

    if env_mod is not None:
        assert isinstance(env_mod, dict)
        for env_name, env_value in env_mod.items():
            if env_value is not None:
                assert isinstance(env_value, str)
                env[env_name] = env_value
            elif env_name in env:
                del env[env_name]

    return env


def wait_on_files(pid, wait_files, poll_rate=0.1, recursive=True, timeout=60):
    """
    Wait for wait_files if open by a process (pid) and it's children (if recursive) to close.

    @type pid: int
    @param pid: pid of process

    @type wait_files: list
    @param wait_files: a list of files that should no longer be open by the process

    @type poll_rate: float
    @param poll_rate: Amount of time in seconds to wait between checks

    @type recursive: bool
    @return: Scan all children and grandchildren for open files

    @type timeout: float
    @param timeout: Amount of time in seconds to poll

    @rtype: bool
    @return: True if all files were closed within timeout else False
    """

    assert poll_rate >= 0, "Invalid poll_rate %d, must be greater than or equal to 0" % poll_rate
    assert timeout >= 0, "Invalid timeout %d, must be greater than or equal to 0" % timeout
    assert poll_rate <= timeout, "poll_rate must be less then or equal to timeout"
    wait_end = time.time() + timeout
    # call realpath() and normcase() on each file for cross platform compatibility
    wait_files = [os.path.normcase(os.path.realpath(x)) for x in wait_files if os.path.exists(x)]
    try:
        proc = psutil.Process(pid)
        while proc.is_running() and wait_files:
            try:
                # only include children and grandchildren if recursive is specified
                children = proc.children(recursive=True) if recursive else list()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                children = list()
            open_files = list()
            for target in [proc] + children:
                try:
                    open_files.extend([x.path for x in target.open_files()])
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            open_files = [os.path.normcase(os.path.realpath(x)) for x in set(open_files)]
            # check if open files are in the wait file list
            if not any(x for x in open_files if x in wait_files):
                break
            elif wait_end <= time.time():
                log.debug("Timeout waiting for: %s", ", ".join(x for x in open_files if x in wait_files))
                return False
            time.sleep(poll_rate)
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass
    return True
