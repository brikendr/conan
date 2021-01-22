import os
import platform


from conans.client import tools
from conans.client.build import defs_to_string, join_arguments
from conans.client.build.autotools_environment import AutoToolsBuildEnvironment
from conans.client.build.cppstd_flags import cppstd_from_settings
from conans.client.tools.env import environment_append, _environment_add
from conans.client.tools.oss import args_to_string, get_build_os_arch
from conans.errors import ConanException
from conans.model.build_info import DEFAULT_BIN, DEFAULT_INCLUDE, DEFAULT_LIB
from conans.model.version import Version
from conans.util.conan_v2_mode import conan_v2_behavior
from conans.util.files import decode_text, get_abs_path, mkdir
from conans.util.runners import version_runner


class Meson(object):

    def __init__(self, conanfile, backend=None, build_type=None, append_vcvars=False, exe_wrapper=None):
        """
        :param conanfile: Conanfile instance (or settings for retro compatibility)
        :param backend: Generator name to use or none to autodetect.
               Possible values: ninja,vs,vs2010,vs2015,vs2017,xcode
        :param build_type: Overrides default build type comming from settings
        :param exe_wrapper: Tells meson what exe wrapper to use eg wine
        """
        self._conanfile = conanfile
        self._settings = conanfile.settings
        self._append_vcvars = append_vcvars
        self.exe_wrapper='false'
        self.exe_wrapper = exe_wrapper or self.exe_wrapper
        self._os = self._ss("os")
        self._compiler = self._ss("compiler")
        if not self._compiler:
            conan_v2_behavior("compiler setting should be defined.",
                              v1_behavior=self._conanfile.output.warn)

        self._compiler_version = self._ss("compiler.version")

        self._build_type = self._ss("build_type")

        self.backend = backend or "ninja"  # Other backends are poorly supported, not default other.

        self.options = dict()
        if self._conanfile.package_folder:
            self.options['prefix'] = self._conanfile.package_folder
        self.options['libdir'] = DEFAULT_LIB
        self.options['bindir'] = DEFAULT_BIN
        self.options['sbindir'] = DEFAULT_BIN
        self.options['libexecdir'] = DEFAULT_BIN
        self.options['includedir'] = DEFAULT_INCLUDE

        # C++ standard
        cppstd = cppstd_from_settings(self._conanfile.settings)
        cppstd_conan2meson = {
            '98': 'c++03', 'gnu98': 'gnu++03',
            '11': 'c++11', 'gnu11': 'gnu++11',
            '14': 'c++14', 'gnu14': 'gnu++14',
            '17': 'c++17', 'gnu17': 'gnu++17',
            '20': 'c++1z', 'gnu20': 'gnu++1z'
        }

        if cppstd:
            self.options['cpp_std'] = cppstd_conan2meson[cppstd]

        # shared
        shared = self._so("shared")
        self.options['default_library'] = "shared" if shared is None or shared else "static"

        # fpic
        if self._os and "Windows" not in self._os:
            fpic = self._so("fPIC")
            if fpic is not None:
                shared = self._so("shared")
                self.options['b_staticpic'] = "true" if (fpic or shared) else "false"

        self.build_dir = None
        if build_type and build_type != self._build_type:
            # Call the setter to warn and update the definitions if needed
            self.build_type = build_type

    def _ss(self, setname):
        """safe setting"""
        return self._conanfile.settings.get_safe(setname)

    def _so(self, setname):
        """safe option"""
        return self._conanfile.options.get_safe(setname)

    @property
    def build_type(self):
        return self._build_type

    @build_type.setter
    def build_type(self, build_type):
        settings_build_type = self._settings.get_safe("build_type")
        if build_type != settings_build_type:
            self._conanfile.output.warn(
                'Set build type "%s" is different than the settings build_type "%s"'
                % (build_type, settings_build_type))
        self._build_type = build_type

    @property
    def build_folder(self):
        return self.build_dir

    @build_folder.setter
    def build_folder(self, value):
        self.build_dir = value

    def _get_dirs(self, source_folder, build_folder, source_dir, build_dir, cache_build_folder):
        if (source_folder or build_folder) and (source_dir or build_dir):
            raise ConanException("Use 'build_folder'/'source_folder'")

        if source_dir or build_dir:  # OLD MODE
            build_ret = build_dir or self.build_dir or self._conanfile.build_folder
            source_ret = source_dir or self._conanfile.source_folder
        else:
            build_ret = get_abs_path(build_folder, self._conanfile.build_folder)
            source_ret = get_abs_path(source_folder, self._conanfile.source_folder)

        if self._conanfile.in_local_cache and cache_build_folder:
            build_ret = get_abs_path(cache_build_folder, self._conanfile.build_folder)

        return source_ret, build_ret

    @property
    def flags(self):
        return defs_to_string(self.options)

    def _configure_cross_compile(self, cross_filename, environ_append):

        arm=('arm','arm','little')
        cpu_translate = {
            'x86' : ('x86', 'x86', 'little'),
            'x86_64' : ('x86_64', 'x86_64', 'little'),
            'x86' : ('x86','x86','little'),
            'ppc32be' : ('ppc','ppc','big'),
            'ppc32' : ('ppc','ppc','little'),
            'ppc64le' : ('ppc64','ppc64','little'),
            'ppc64' : ('ppc64','ppc64','big'),
            'armv4' : arm,
            'armv4i' : arm,
            'armv5el' : arm,
            'armv5hf' : arm,
            'armv6' : arm,
            'armv7' : arm,
            'armv7hf' : arm,
            'armv7s' : arm,
            'armv7k' : arm,
            'armv8_32' : arm,
            'armv8' : ('aarch64', 'aarch64', 'little'),
            'armv8.3' : ('aarch64', 'aarch64', 'little'),
            'sparc' : ('sparc','sparc','big'),
            'sparcv9' : ('sparc64','sparc64','big'),
            'mips' : ('mips','mips','big'),
            'mips64' : ('mips64','mips64','big'),
            'avr' : ('avr','avr','little'),
            's390' : ('s390','s390','big'),
            's390x' : ('s390','s390','big'),
            'wasm' : ('wasm','wasm','little'),
        }
        if hasattr(self._conanfile,'settings_build'):
            build_cpu_family, build_cpu, build_endian = cpu_translate[str(self._conanfile.settings_build.arch)]
            os_build, _ = get_build_os_arch(self._conanfile)

        os_host = str(self._conanfile.settings.os).lower()
        cpu_family, cpu, endian = cpu_translate[str(self._conanfile.settings.arch)]

        cflags = ', '.join(repr(x) for x in os.environ.get('CFLAGS', '').split(' '))
        cxxflags = ', '.join(repr(x) for x in os.environ.get('CXXFLAGS', '').split(' '))

        cc = os.environ.get('CC', 'cc')
        cpp = os.environ.get('CXX', 'c++')
        ld = os.environ.get('LD', 'ld')
        ar = os.environ.get('AR', 'ar')
        strip = os.environ.get('STRIP', 'strip')
        ranlib = os.environ.get('RANLIB', 'ranlib')
        libdir = environ_append['PKG_CONFIG_PATH']

        with open(cross_filename, "w") as fd:
            if hasattr(self._conanfile,'settings_build') :
                fd.write("""
                    [build_machine]
                    system = '{os_build}'
                    cpu_family = '{build_cpu_family}'
                    cpu = '{build_cpu}'
                    endian = '{build_endian}'
                """.format(
                    os_build = os_build.lower(),
                    build_cpu_family = build_cpu_family,
                    build_cpu = build_cpu,
                    build_endian = build_endian,
                ))
            fd.write("""
                [host_machine]
                system = '{os_host}'
                cpu_family = '{cpu_family}'
                cpu = '{cpu}'
                endian = '{endian}'

                [properties]
                needs_exe_wrapper = '{exe_wrapper}'
                cpp_args = [{cxxflags}]
                c_args = [{cflags}]
                pkg_config_libdir='{libdir}'

                [binaries]
                c = '{cc}'
                cpp = '{cpp}'
                ar = '{ar}'
                ld = '{ld}'
                strip = '{strip}'
                ranlib = '{ranlib}'
                pkgconfig = 'pkg-config'
                """.format(
                    os_host = os_host,
                    cpu_family = cpu_family,
                    cpu = cpu,
                    endian = endian,
                    exe_wrapper = self.exe_wrapper,
                    cxxflags = cxxflags,
                    cflags = cflags,
                    libdir = libdir,
                    cc = cc,
                    cpp = cpp,
                    ar = ar,
                    ld = ld,
                    strip = strip,
                    ranlib = ranlib,
                ))
        environ_append.update({'CC': None,
                               'CXX': None,
                               'CFLAGS': None,
                               'CXXFLAGS': None,
                               'CPPFLAGS': None,
                               'LDFLAGS':None,
                               })

    def configure(self, args=None, defs=None, source_dir=None, build_dir=None,
                  pkg_config_paths=None, cache_build_folder=None,
                  build_folder=None, source_folder=None,no_generated_cross_file=None):
        if not self._conanfile.should_configure:
            return
        args = args or []
        defs = defs or {}

        # overwrite default values with user's inputs
        self.options.update(defs)

        source_dir, self.build_dir = self._get_dirs(source_folder, build_folder,
                                                    source_dir, build_dir,
                                                    cache_build_folder)

        if pkg_config_paths:
            pc_paths = os.pathsep.join(get_abs_path(f, self._conanfile.install_folder)
                                       for f in pkg_config_paths)
        else:
            pc_paths = self._conanfile.install_folder

        mkdir(self.build_dir)

        bt = {"RelWithDebInfo": "debugoptimized",
              "MinSizeRel": "release",
              "Debug": "debug",
              "Release": "release"}.get(str(self.build_type), "")

        build_type = "--buildtype=%s" % bt
        cross_option = None
        environ_append = {"PKG_CONFIG_PATH": pc_paths}
        if tools.cross_building(self._conanfile.settings):
            if not no_generated_cross_file:
                cross_filename = os.path.join(self.build_dir, "cross_file.txt")
                cross_option = "--cross-file=%s" % cross_filename
                self._configure_cross_compile(cross_filename, environ_append)

        arg_list = join_arguments([
            cross_option,
            "--backend=%s" % self.backend,
            self.flags,
            args_to_string(args),
            build_type,
        ])
        command = 'meson "%s" "%s" %s' % (source_dir, self.build_dir, arg_list)
        with environment_append(environ_append):
            self._run(command)

    @property
    def _vcvars_needed(self):
        return (self._compiler == "Visual Studio" and self.backend == "ninja" and
                platform.system() == "Windows")

    def _run(self, command):
        def _build():
            env_build = AutoToolsBuildEnvironment(self._conanfile)
            with environment_append(env_build.vars):
                self._conanfile.run(command)

        if self._vcvars_needed:
            vcvars_dict = tools.vcvars_dict(self._settings, output=self._conanfile.output)
            with _environment_add(vcvars_dict, post=self._append_vcvars):
                _build()
        else:
            _build()

    def _run_ninja_targets(self, args=None, build_dir=None, targets=None):
        if self.backend != "ninja":
            raise ConanException("Build only supported with 'ninja' backend")

        args = args or []
        build_dir = build_dir or self.build_dir or self._conanfile.build_folder

        arg_list = join_arguments([
            '-C "%s"' % build_dir,
            args_to_string(args),
            args_to_string(targets)
        ])
        self._run("ninja %s" % arg_list)

    def _run_meson_command(self, subcommand=None, args=None, build_dir=None):
        args = args or []
        build_dir = build_dir or self.build_dir or self._conanfile.build_folder

        arg_list = join_arguments([
            subcommand,
            '-C "%s"' % build_dir,
            args_to_string(args)
        ])
        self._run("meson %s" % arg_list)

    def build(self, args=None, build_dir=None, targets=None):
        if not self._conanfile.should_build:
            return
        if not self._build_type:
            conan_v2_behavior("build_type setting should be defined.",
                              v1_behavior=self._conanfile.output.warn)
        self._run_ninja_targets(args=args, build_dir=build_dir, targets=targets)

    def install(self, args=None, build_dir=None):
        if not self._conanfile.should_install:
            return
        mkdir(self._conanfile.package_folder)
        if not self.options.get('prefix'):
            raise ConanException("'prefix' not defined for 'meson.install()'\n"
                                 "Make sure 'package_folder' is defined")
        self._run_ninja_targets(args=args, build_dir=build_dir, targets=["install"])

    def test(self, args=None, build_dir=None, targets=None):
        if not self._conanfile.should_test:
            return
        if not targets:
            targets = ["test"]
        self._run_ninja_targets(args=args, build_dir=build_dir, targets=targets)

    def meson_install(self, args=None, build_dir=None):
        if not self._conanfile.should_install:
            return
        self._run_meson_command(subcommand='install', args=args, build_dir=build_dir)

    def meson_test(self, args=None, build_dir=None):
        if not self._conanfile.should_test:
            return
        self._run_meson_command(subcommand='test', args=args, build_dir=build_dir)

    @staticmethod
    def get_version():
        try:
            out = version_runner(["meson", "--version"])
            version_line = decode_text(out).split('\n', 1)[0]
            version_str = version_line.rsplit(' ', 1)[-1]
            return Version(version_str)
        except Exception as e:
            raise ConanException("Error retrieving Meson version: '{}'".format(e))
