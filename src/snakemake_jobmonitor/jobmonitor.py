import os
from os import path as op
from datetime import datetime
import traceback
import subprocess
import re

def string2token(s):
    def to0x(c):
        x = ord(c.group())
        if x>255:
            x = 168 # inverted questionmark
        return f'0x{x:02x}'

    if re.search(r'[^a-zA-Z0-9]',s):
        s = s.replace('0x','0xx')
        s = '('+re.sub(r'[^a-zA-Z0-9_\-. ]',to0x,s)+')'
    return s

def token2string(s):
    def toChar(xx):
        x = bytearray.fromhex(xx.group())
        return x.decode()

        x = ord(c.group())
        if x>255:
            x = 168 # inverted questionmark
        return f'0x{x:02x}'

    if s[0] == '(':
        s = re.sub(r'0x[a-fA-F0-9]{2}',toChar,s[1:-1])
        s = s.replace('0xx','0x')
    return s
    

# Tracks job progress in a text file that must have the extension `.log`.
# During execution, the extension is changed to `.running`.
# On completion the extension is changed back to `.log`, even if an error occurs.
# In that case the error message is written to an additional file with the extension `.error`.
class JobMonitor():
    def __init__(self,jobLogFile,jobName='Job'):
        if not jobLogFile.endswith('.log'):
            raise RuntimeError(f'jobLogFile ({jobLogFile}) must have the extension ".log"')
        self.logFile = f'{jobLogFile[:-4]}.running'
        self.jobName = jobName
        

    def __enter__(self):
        self.start = datetime.now()
        os.makedirs(os.path.dirname(self.logFile),exist_ok=True)
        with open(self.logFile,'wt') as fp:
            fp.write(f'"{self.jobName}" started at {self.start}.\n')

        # if process failed previously, remove the failure report
        oldFail = self.logFile[:-8]+'.error'
        if os.path.exists(oldFail):
             os.remove(oldFail)

        return self

        
    def __exit__(self, exc_type, exc_value, tb):
        elapsed = datetime.now()-self.start
        if exc_type is None:
            # Process is ready.
            self.log(f'"{self.jobName}" completed in {elapsed} hh:mm:ss.')
        else:
            # An error occured. Report it.
            err = traceback.format_exception(exc_type, exc_value, tb)
            if isinstance(err,list):
                err = "\n".join(err)
            self.log(f'"{self.jobName}" failed after {elapsed} hh:mm:ss.')
            self.log(f'Error message: {err}',timeIt=False)
            # create an error message file
            errorFile = self.logFile[:-8]+'.error'
            with open(errorFile,'wt') as fp:
                fp.write(err)
            
        os.rename(self.logFile,self.logFile[:-8]+'.log')
        return True

            
    # fail this job if it depends on another failed job
    def checkDependency(self,dependencyLog):
        errorFile = dependencyLog[0:-4]+'.error'
        assert not op.isfile(errorFile), f'"{self.jobName}" failed because it depends on another failed job: `{errorFile}`.'
        

    # Check whether all upstream dependencies completed without error;
    # only files ending with .log are be checked.
    def checkDependencies(self,dependencies):
        for dep in dependencies:
            if dep.endswith('.log'):
                self.checkDependency(dep)

        
    def log(self,msg,timeIt=True):
        with open(self.logFile,'at') as fp:
            if timeIt:
                elapsed = datetime.now()-self.start
                fp.write(f'[{elapsed}] {msg}\n')
            else:
                fp.write(f'{msg}\n')
                
    def run(self,cmd,cwd=None, timeout=None, env=None):
        self.log(f'Running process `{subprocess.list2cmdline(cmd)}`')
        p = subprocess.run(cmd,cwd=cwd,timeout=timeout,env=env,capture_output=True,text=True)
        self.log(p.stdout)
        if p.returncode>0:
            raise RuntimeError(p.stderr)
        
        
