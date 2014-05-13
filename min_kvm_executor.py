#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Use minimal KVM system: kernel image, minimal filesystem with busybox.
"""

from __future__ import with_statement


# Use simplejson or Python 2.6 json, prefer simplejson.
try:
    import simplejson as json
except ImportError:
    import json

import os
import sys
import time
import logging
import signal
import shutil
import ConfigParser
from threading import Thread
from subprocess import Popen
import serial
from subprocess import Popen, PIPE, STDOUT
from vmchecker.config import VmwareMachineConfig, CourseConfig, VmwareConfig
from vmchecker.generic_executor import VM, Host
_logger = logging.getLogger('vm_executor')

KVM_VM_PATH = "/home/vmchecker/so2/vm/qemu-vm"
KVM_VM_FS_PATH = "/home/vmchecker/so2/vm/qemu-vm/fsimg"
KVM_VM_CLEAN_FS_PATH = "/home/vmchecker/so2/vm/qemu-vm/fsimg-clean-slate"

class min_kvmHost(Host):
    def getVM(self, bundle_dir, vmcfg, assignment):
        return min_kvmVM(self, bundle_dir, vmcfg, assignment)

class min_kvmVM(VM):
    hostname = 'min-kvm'
    def __init__(self, host, bundle_dir, vmcfg, assignment):
        VM.__init__(self, host, bundle_dir, vmcfg, assignment)
        self.hostname = self.machinecfg.get_vmx_path()

    def runBuildScript(self, targetdir):
        os.chdir(targetdir)
        self.host.executeCommand("/bin/sh ./build.sh")

    def addTestRun(self, targetdir):
        with open(KVM_VM_FS_PATH + "/etc/rcS", "a") as f:
            f.write("\n")
            f.write("dmesg -n 8\n")
            f.write("dmesg -c > /dev/null 2>&1\n")
            f.write("sleep 3\n")
            f.write("cd " + targetdir + "\n")
            f.write("/bin/sh ./run.sh\n")
            f.write("sleep 5\n")
            f.write("rmmod netconsole\n")
            f.write("cat run-stdout.vmr | nc 172.20.0.1 6777\n")
            f.write("cat run-stderr.vmr | nc 172.20.0.1 6888\n")
            f.write("poweroff\n")

    def copyTo(self, sourceDir, targetDir, files):
        """ Copy files from host(source) to temporary build folder. """

        # Copy all files to guest filesystem.
        for f in files:
            host_path = os.path.join(sourceDir, f)
            guest_path = os.path.join(targetDir, f)
            guest_path_in_host = KVM_VM_FS_PATH + guest_path
            if not os.path.exists(host_path):
                _logger.error('host file (to send) "%s" does not exist' % host_path)
                return
            _logger.info('copy file %s from host to guest folder in host at %s' % (host_path, guest_path_in_host))
            shutil.copyfile(host_path, guest_path_in_host)

    def copyFrom(self, sourceDir, targetDir, files):
        for f in files:
            src_path = os.path.join(KVM_VM_FS_PATH, "root", f)
            dest_path = os.path.join(targetDir, f)
            _logger.info('copy file %s to %s' % (src_path, dest_path))
            shutil.copyfile(src_path, dest_path)

    def run(self, shell, executable_file, timeout):
        # Kill qemu VM process if any.
        self.host.executeCommand("pkill -f qemu-system-i386")

        # Kill netcat processes, if any.
        self.host.executeCommand("pkill -f nc.openbsd")

        # No os.path fanciness, since Windows has no /dev/zero anyway.
        fin_zero = open("/dev/zero")

        # Listen on UDP for netconsole messages.
        fout_kernel = open(os.path.join(KVM_VM_FS_PATH, "root", "run-km.vmr"), "wt")
        nckern_process = Popen(["/bin/nc.openbsd", "-ulp", "6666"], stdout=fout_kernel, stderr=STDOUT)

        # Read run-stdout.vmr.
        fout_run = open(os.path.join(KVM_VM_FS_PATH, "root", "run-stdout.vmr"), "wt")
        ncout_process = Popen(["/bin/nc.openbsd", "-lp", "6777"], stdout=fout_run, \
                        stderr=STDOUT, stdin=fin_zero)

        # Read run-stderr.vmr.
        ferr_run = open(os.path.join(KVM_VM_FS_PATH, "root", "run-stderr.vmr"), "wt")
        ncerr_process = Popen(["/bin/nc.openbsd", "-lp", "6888"], stdout=ferr_run, \
                        stderr=STDOUT, stdin=fin_zero)

        # Start minimalistic KVM virtual machine and wait for its completion.
        os.chdir(KVM_VM_PATH)
        _logger.debug('Starting virtual machine')
        vmprocess = Popen(["make"],stdout=PIPE,stderr=STDOUT)
        output = vmprocess.stdout.read()
        _logger.debug('Command output: %s' % output)

        _logger.debug('run timeout: %s' % timeout)
        # Wait for task to complete or wait for timeout.
        for i in range(0, timeout-1, 5):
            time.sleep(5)
            # Poll for qemu-system-i386 process.
            with open(os.devnull, "w") as fnull:
                pgrep_process = Popen(["/usr/bin/pgrep", "-f", "qemu-system-i386"], stdout=fnull, stderr=fnull)
                pgrep_process.wait()
                # If `qemu-system-i386' process does not exist, get out.
                if (pgrep_process.returncode != 0):
                    _logger.debug("qemu-system-i386 process does not exist")
                    break

        # Terminate all processes.
        self.host.executeCommand("pkill -9 -f qemu-system-i386")
        #self.host.executeCommand("pkill -9 -f nttcp")
        nckern_process.kill()
        ncout_process.kill()
        ncerr_process.kill()
        fout_kernel.close()
        fout_run.close()
        ferr_run.close()
        fin_zero.close()

    def runTest(self, bundle_dir, machinecfg, test):
        try:
            # FIXME: It's a hack. We only clean the fs when running
            # the build script. It should be once no matter what.
            if test['script'][0] == 'build.sh':
                # Restore clean slate file system image.
                shutil.rmtree(KVM_VM_FS_PATH, ignore_errors=True)
                shutil.copytree(KVM_VM_CLEAN_FS_PATH, KVM_VM_FS_PATH)

            files_to_copy = test['input'] + test['script']
            guest_dest_dir = machinecfg.guest_base_path()
            self.copyTo(bundle_dir, guest_dest_dir, files_to_copy)
            guest_path_in_host = KVM_VM_FS_PATH + guest_dest_dir

            # Run build script for kernel modules and tests.
            if test['script'][0] == 'build.sh':
                self.runBuildScript(guest_path_in_host)
                self.copyFrom(guest_dest_dir, bundle_dir,test['output'])

            elif test['script'][0] == 'run.sh':
                # Append test run scripts to rcS file.
                self.addTestRun(guest_dest_dir)

                # Run actual tests.
                shell = machinecfg.guest_shell_path()
                dest_in_guest_shell = machinecfg.guest_home_in_shell()
                script_in_guest_shell = dest_in_guest_shell  + 'run.sh'
                _logger.debug('runTest timeout: %s' % test['timeout'])
                timedout = self.run(shell, script_in_guest_shell, test['timeout'])
                self.copyFrom(guest_dest_dir,bundle_dir,test['output'])
                if timedout:
                    return False
        except:
            _logger.exception('error in copy_files_and_run_script')
        finally:
            return True
