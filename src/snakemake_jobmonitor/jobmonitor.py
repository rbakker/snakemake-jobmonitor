import os
from os import path as op
from datetime import datetime
import traceback
import subprocess
import re
import logging
import json

logger = logging.getLogger('job')
logging.basicConfig(encoding='utf-8', level=logging.INFO)

"""
def snakemakeLogHandler(logFile,logLevel=logging.DEBUG):
    formatter = logging.Formatter("%(asctime)s [%(levelname)-5s|%(name)s] %(message)s")
    logger = logging.getLogger(f'snakemake.jobmonitor')
    handler = logging.FileHandler(logFile)
    handler.setLevel(logLevel)
    handler.setFormatter(formatter)
    logger.addHandler(handler)    
"""

# utility function to convert a string into a token that is safe for inclusion in file names.
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

# utility function to convert a token back into the string that it was derived from.
def token2string(s):
    def toChar(xx):
        x = bytearray.fromhex(xx.group(1))
        return x.decode()

        x = ord(c.group())
        if x>255:
            x = 168 # inverted questionmark
        return f'0x{x:02x}'

    if s[0] == '(':
        s = re.sub(r'0x([a-fA-F0-9]{2})',toChar,s[1:-1])
        s = s.replace('0xx','0x')
    return s
    

# Apply substitutions in a dictionary that contains wildcard keys,
# like "{subject}":"s123"
# In this case, all instances of {subject} in descendant nodes will be
# substituted by the wildcard value "s123" 
def applySubstitutions(kv,wildcards={}):
    for k,v in kv.items():
        if isinstance(v,str):
            try:
                v = v.format(**wildcards)
                kv[k] = v
            except Exception as E:
                print('Exception in applySubstitutions',E)
                pass

        if k.startswith('{') and k.endswith('}'):
            wildcards[k[1:-1]] = v
        
        if isinstance(v,dict):
            v = applySubstitutions(v,wildcards.copy())
    
    return kv
    
   
# JobResult manages the result storage location.
# If `logFile` does not yet exist, a `prefix` is needed to know where result files are stored.
# Otherwise, `prefix` is obtained from the 2nd line of the log file.
class JobResult():
    def __init__(self,logFile,prefix=None,makedirs=False):
        if prefix:
            self.prefix = prefix
        else:
            with open(logFile,'rt') as fp:
                fp.readline()
                self.prefix = fp.readline().rstrip('* \n')
        self.logFile = logFile
        self.makedirs = makedirs
    
    # Short-hand version of the file() method.
    def __call__(self,*args):
        return self.file(*args)
        
    # Return a file in the job's output folder, including the prefix.
    # Use multiple arguments for files in subfolders.
    # Examples:
    #   if prefix is '/path/to/workdir/', file('result.txt') returns /path/to/workdir/result.txt
    #   if prefix is '/path/to/workdir/caseA_', file('result.txt') returns /path/to/workdir/caseA_result.txt
    #   if prefix is '/path/to/workdir/caseA_', file('step1','result.txt') returns /path/to/workdir/caseA_step1/result.txt
    def file(self,*args):
        if len(args):
            resultFile = op.join(self.prefix+args[0],*args[1:])
        else:
            resultFile = self.prefix
        if self.makedirs:
            os.makedirs(op.dirname(resultFile),exist_ok=True)
        return resultFile
    
    # Return the folder name of a job result file, see the file() method.
    def folder(self,*args):
        return op.dirname(self.file(*args))

    # Read the file given by *args and return its json-decoded contents.
    def parseJson(self,*args):
        with open(self.file(*args),'rt') as fp:
            return json.load(fp)
                
    # Return file that contains error message for this job, if any.
    def error():
        errorFile = op.splitext(self.logFile)[0]+'.error'
        return errorFile if op.isfile(errorFile) else None
        
    def success(self):
        return not self.error()
        

# JobMonitor tracks job progress and logs messages.
# It has a `run` method to use shell commands and capture their output in the log file.
#
# `logFile` is the file that contains the job log. 
# During job execution, the file extension will be modified into '.running'.
# On completion its extension is changed back to `.log`, even if an error occurs.
# Error messages are written to an additional file with the extension `.error`.
#
# `jobName` is a short descriptive name for the job, used in error messages and in the log file.
#
# `resultFolder` is the location where results are stored. Default: folder of logFile.
# If it ends with '*' as in /my/folder/subject01* then results are stored in '/my/folder' with filenames starting with 'subject01'.
#
class JobMonitor():
    def __init__(self,logFile,jobName='Job',resultFolderOrPrefix=None):
        if not isinstance(logFile,str):
            logFile = logFile[0]
        self.logNoext,self.logExt = op.splitext(logFile)
        self.logFile = self.logNoext+'.running'
        self.jobName = jobName

        # fullPrefix is the combination of resultFolder and resultPrefix
        if not resultFolderOrPrefix:
            resultFolderOrPrefix = op.dirname(logFile)
        if resultFolderOrPrefix.endswith('*'):
            prefix = resultFolderOrPrefix[:-1]
        else:
            prefix = op.join(resultFolderOrPrefix,'')
            
        self.result = JobResult(logFile,prefix=prefix,makedirs=True)
            

    def startLogging(self):
        # log using the Python logging module
        formatter = logging.Formatter("%(asctime)s [%(levelname)-5s|%(name)s] %(message)s")
        logger = logging.getLogger(f'job.{self.jobName}')
        handler = logging.FileHandler(self.logFile)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        self.logger = logger
        self.handler = handler


    def stopLogging(self):
        self.logger.removeHandler(self.handler)


    def __enter__(self):
        self.start = datetime.now()
        logFolder = op.dirname(self.logFile)
        os.makedirs(logFolder,exist_ok=True)
            
        with open(self.logFile,'wt') as fp:
            fp.write(f'"{self.jobName}" started at {self.start}, saving output to\n{self.result.file()}\n')

        try:
            # if process failed previously, remove the failure report
            oldFail = op.splitext(self.logFile)[0]+'.error'
            if op.exists(oldFail):
                 os.remove(oldFail)
            
            # No longer necessary with the makedirs option in JobResult
            #if self.result:
            #    # make sure result folder exists
            #    os.makedirs(self.result.folder(),exist_ok=True)

            self.startLogging()
        except Exception as e:
            tb = traceback.format_exc().splitlines()
            raise RuntimeError(f'Error in JobMonitor, {tb}')
            
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
            self.error(err)
        
        self.stopLogging()
        os.rename(self.logFile,self.logNoext+self.logExt)
            
        return True

            
    # return error message, if any
    def checkError(self,logFile=None):
        if not isinstance(logFile,str):
            logFile = logFile[0]
        if not logFile:
            logFile = self.logFile
        errorFile = op.splitext(logFile)[0]+'.error'
        if op.isfile(errorFile):
            with open(errorFile,'rt') as fp:
                return fp.read()
        

    # fail this job if it depends on another failed job
    def checkDependency(self,logFile):
        error = self.checkError(logFile)
        assert not error, f'"{self.jobName}" failed because it depends on another job `{op.splitext(logFile)[0]}`,\nwhich failed due to:\n{error}'


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


    def error(self,msg):
        self.log(f'Error message: {msg}',timeIt=False)
        # create an error message file
        errorFile = op.splitext(self.logFile)[0]+'.error'
        with open(errorFile,'at') as fp:
            fp.write(msg+'\n')


    def run(self,cmd,cwd=None, timeout=None, env=None, failOnError=True, liveUpdates=False):
        msg = f'Running process `{subprocess.list2cmdline(cmd)}`'
        self.log(msg)
        if liveUpdates:
            print(f'{msg},\n=> output to {self.logFile}.')
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE, text=True)
     
            line = p.stdout.readline()
            while line:
                msg = line.rstrip('\n')
                self.log(msg,timeIt=False)
                line = p.stdout.readline()
            
            returnCode = p.wait()
            if p.returncode>0:
                stderr = p.stderr.read()
        else:            
            p = subprocess.run(cmd,cwd=cwd,timeout=timeout,env=env,capture_output=True,text=True)
            self.log(p.stdout)
            returnCode = p.returncode
            stderr = p.stderr
            
        if returnCode>0:
            if failOnError:
                # report the error and raise exception
                raise RuntimeError(stderr)
            else:
                # just report the error
                self.error(stderr)
        
        
    def runVerbose(self,cmd,cwd=None, env=None, failOnError=True):
        #self.run(cmd,env,failOnError=failOnError,liveUpdates=True)
        self.log(f'Running process `{subprocess.list2cmdline(cmd)}`')
        print(f'Running process `{subprocess.list2cmdline(cmd)}`,\nsaving output to {self.logFile}.')
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE, text=True)
 
        line = p.stdout.readline()
        while line:
            msg = line.rstrip('\n')
            self.log(msg,timeIt=False)
            line = p.stdout.readline()
        
        returnCode = p.wait()
        if p.returncode>0:
            stderr = p.stderr.read()
            if failOnError:
                # report the error and raise exception
                raise RuntimeError(stderr)
            else:
                # just report the error
                self.error(stderr)
                
                
    def shellWrap(self,shellScript):
        return 'set +euo pipefail; exec >> {job.logFile:q} 2>&1; '+shellScript
