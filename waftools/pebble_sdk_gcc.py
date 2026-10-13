# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from waflib.Errors import BuildError

import inject_metadata


def configure(conf):
    """
    This method is called from the configure method of the pebble_sdk waftool to setup the
    environment variables for compiling a 3rd party app
    :param conf: the ConfigContext
    :return: None
    """
    CROSS_COMPILE_PREFIX = 'arm-none-eabi-'

    conf.env.AS = CROSS_COMPILE_PREFIX + 'gcc'
    conf.env.AR = CROSS_COMPILE_PREFIX + 'ar'
    conf.env.CC = CROSS_COMPILE_PREFIX + 'gcc'
    conf.env.LD = CROSS_COMPILE_PREFIX + 'ld'
    conf.env.SIZE = CROSS_COMPILE_PREFIX + 'size'

    optimize_flag = '-Os'

    conf.load('gcc')

    pebble_cflags = ['-std=c99',
                     '-mcpu=cortex-m3',
                     '-mthumb',
                     '-ffunction-sections',
                     '-fdata-sections',
                     '-g',
                     '-fPIE',
                     optimize_flag]
    c_warnings = ['-Wall',
                  '-Wextra',
                  '-Werror',
                  '-Wno-unused-parameter',
                  '-Wno-error=unused-function',
                  '-Wno-error=unused-variable',
                  # Same modern-gcc-vs-decade-old-code compat flags applied
                  # to the firmware toolchain in pebble_arm_gcc.py: this is
                  # a separate compiler env (for 3rd-party/stored apps) and
                  # doesn't inherit those flags, but hits the same issues.
                  '-Wno-builtin-macro-redefined',
                  '-Wno-error=address-of-packed-member',
                  '-Wno-error=array-bounds',
                  '-Wno-error=return-local-addr',
                  # Generated pebble.h redeclares libc functions like
                  # strftime/time with the app SDK's own historical
                  # signatures (built against an older libc where e.g.
                  # strftime returned int); modern gcc's builtin prototypes
                  # disagree and -Werror would turn that into a hard error.
                  '-Wno-error=builtin-declaration-mismatch',
                  '-Wno-error=enum-conversion',
                  '-Wno-error=restrict',
                  '-Wno-error=implicit-fallthrough',
                  '-Wno-error=unused-const-variable',
                  '-Wno-error=array-parameter',
                  '-Wno-error=format-truncation',
                  '-Wno-error=cast-function-type',
                  '-Wno-error=sizeof-pointer-memaccess',
                  '-Wno-error=deprecated',
                  '-Wno-error=expansion-to-defined',
                  '-Wno-error=stringop-truncation']

    if (conf.env.SDK_VERSION_MAJOR == 5) and (conf.env.SDK_VERSION_MINOR > 19):
        # pebble.h declares its own struct tm/strftime/localtime/etc
        # (the app SDK's historical, stripped-down time API), which
        # collide with the real newlib <time.h> if it's allowed to
        # declare them too -- hence predefining newlib's _TIME_H_ guard
        # so pebble.h's #include <time.h> is a no-op for everything time.h
        # would otherwise add. But that guard also gates newlib's own
        # #include <sys/types.h>, which is where time_t itself actually
        # gets typedef'd (under its own separate __time_t_defined guard) --
        # so without forcing that in first, time_t ends up undeclared.
        # Force it in explicitly so pebble.h's time_t usages still resolve.
        pebble_cflags.append('-D_TIME_H_')
        pebble_cflags.append('-include')
        pebble_cflags.append('sys/types.h')
    pebble_cflags.extend(c_warnings)

    pebble_linkflags = ['-mcpu=cortex-m3',
                        '-mthumb',
                        '-Wl,--gc-sections',
                        '-Wl,--warn-common',
                        '-fPIE',
                        optimize_flag]

    conf.env.prepend_value('CFLAGS', pebble_cflags)
    conf.env.prepend_value('LINKFLAGS', pebble_linkflags)
    conf.env.SHLIB_MARKER = None
    conf.env.STLIB_MARKER = None


# -----------------------------------------------------------------------------------
def gen_inject_metadata_rule(bld, src_bin_file, dst_bin_file, elf_file, resource_file, timestamp,
                             has_pkjs, has_worker):
    """
    Copy from src_bin_file to dst_bin_file and inject the correct meta-data into the
    header of dst_bin_file
    :param bld: the BuildContext
    :param src_bin_file: the path to the pebble-app.raw.bin file
    :param dst_bin_file: the path to the pebble-app.bin
    :param elf_file: the path to the pebble-app.elf file
    :param resource_file: the path to the resource pack
    :param timestamp: the timestamp of the project build
    :param has_pkjs: boolean for whether the project contains code using PebbleKit JS
    :param has_worker: boolean for whether the project has a worker binary
    """

    def inject_data_rule(task):
        bin_path = task.inputs[0].abspath()
        elf_path = task.inputs[1].abspath()
        if len(task.inputs) >= 3:
            res_path = task.inputs[2].abspath()
        else:
            res_path = None
        tgt_path = task.outputs[0].abspath()

        # First copy the raw bin that the compiler produced to a new location. This way we'll have
        # the raw binary around to inspect just in case anything went wrong while we were injecting
        # metadata.
        cp_result = task.exec_command('cp "{}" "{}"'.format(bin_path, tgt_path))
        if cp_result < 0:
            raise BuildError("Failed to copy %s to %s!" % (bin_path, tgt_path))

        # Now actually inject the metadata into the new copy of the binary.
        inject_metadata.inject_metadata(tgt_path, elf_path, res_path, timestamp,
                                        allow_js=has_pkjs, has_worker=has_worker)

    sources = [src_bin_file, elf_file]
    if resource_file is not None:
        sources.append(resource_file)
    bld(rule=inject_data_rule, name='inject-metadata', source=sources, target=dst_bin_file)
