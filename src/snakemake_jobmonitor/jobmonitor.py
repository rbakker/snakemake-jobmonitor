import os
from os import path as op
from datetime import datetime
import traceback
import subprocess
import re
import json
import time
import tempfile
import shlex


# Replaces extension, but keeps extra extension if equal to `outerExt`.
# Returns file with new extension.
def replaceInnerExtension(fname,newExt,outerExt='.md'):
    noext,ext = op.splitext(fname)
    hasOuter = (ext==outerExt)
    if hasOuter:
        noext,ext = op.splitext(noext)
    return noext+newExt+(outerExt if hasOuter else '')


class JobResult():
    def __init__(self, log_file, prefix=None, create=False, output_patterns=None):
        self._log_file = log_file
        self._create = create
        self._output_patterns = output_patterns or {}
        self._compiled_patterns = []
        self._named_outputs = {}
        self._numbered_outputs = []
        self._errors = []
        self._warnings = []
        
        if self._create:
            if prefix:
                self._prefix = prefix
            else:
                self._prefix = op.join(op.dirname(log_file),'')
            if output_patterns:
                self._compile_patterns(output_patterns)
        else:
            if not op.exists(log_file):
                raise FileNotFoundError(f"Log file not found: {log_file}")
                
            with open(log_file, 'rt') as fp:
                fp.readline()
                self._prefix = fp.readline().rstrip('* \n')
            
            mapping = self._load_mapping_from_log()
            if mapping:
                self._apply_mapping(mapping)


    @classmethod
    def fromCheckpoint(cls,checkpoint,wildcards):
        logFile = checkpoint.get(**wildcards).output[0]
        return cls(logFile)
        
      
    def __getattr__(self, name):
        if name in self._named_outputs:
            value = self._named_outputs[name]
        elif name in self._output_patterns:
            value = self._output_patterns[name]
            self._named_outputs[name] = value
        else:
            matched_value = self._match_pattern(name)
            if matched_value:
                value = matched_value
                self._named_outputs[name] = value
            else:
                raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        
        return self._resolve_value(value) if isinstance(value,str) else value


    def _compile_patterns(self, patterns):
        """Pre-compiles keys with {wildcards} into regex objects."""
        for pattern, template in patterns.items():
            if '{' in pattern and '}' in pattern:
                # Escape literal chars (like dots or hyphens)
                regex_str = re.escape(pattern)
                # Re-enable the brackets for wildcard replacement
                regex_str = regex_str.replace(r'\{', '{').replace(r'\}', '}')
                # Convert {name} to named capture groups
                regex_str = re.sub(r'\{(\w+)\}', r'(?P<\1>.+?)', regex_str)
                
                # Store the compiled regex along with the original template
                self._compiled_patterns.append((re.compile(f"^{regex_str}$"), template))


    def _match_pattern(self, name):
        """Uses the pre-compiled cache to find a match."""
        for regex, template in self._compiled_patterns:
            match = regex.match(name)
            if match:
                try:
                    return template.format(**match.groupdict())
                except KeyError:
                    continue
        return None  
              
              
    def __getitem__(self, idx):
        if isinstance(idx, (int, slice)):
            value = self._numbered_outputs[idx]
            return self._resolve_value(value)
        elif isinstance(idx, str):
            return self.__getattr__(idx)
        else:
            raise TypeError(f"Invalid index type: {type(idx).__name__}")


    def __call__(self, arg=None, **kwargs):
        if kwargs:
            name, value = next(iter(kwargs.items()))
            self._named_outputs[name] = value
        elif arg is not None:
            value = arg
            self._numbered_outputs.append(value)
        else:
            value = ''
        
        return self._resolve_value(value)


    def _apply_mapping(self, mapping):
        self._numbered_outputs.extend(mapping.get('by_number', []))
        self._named_outputs.update(mapping.get('by_name', {}))
        self._errors.extend(mapping.get('errors', []))
        self._warnings.extend(mapping.get('warnings', []))


    def _load_mapping_from_log(self):
        try:
            with open(self._log_file, 'rb') as f:
                f.seek(0, os.SEEK_END)
                pointer = f.tell()
                buffer = []
                while pointer > 0:
                    pointer -= 1
                    f.seek(pointer)
                    char = f.read(1).decode('ascii', errors='ignore')
                    buffer.append(char)
                    if char == '{':
                        is_sol = pointer == 0
                        if not is_sol:
                            f.seek(pointer - 1)
                            if f.read(1).decode('ascii', errors='ignore') in ['\n', '\r']:
                                is_sol = True
                        if is_sol:
                            json_str = "".join(reversed(buffer))
                            return json.loads(json_str)
        except (OSError, json.JSONDecodeError):
            return None
        return None


    def _resolve_value(self, value):
        if isinstance(value, (list, tuple)):
            return self._file(*value)
        return self._file(value)


    def _output_mapping(self):
        mapping = dict()
        if self._numbered_outputs:
            mapping['by_number'] = self._numbered_outputs
        if self._named_outputs:
            mapping['by_name'] = self._named_outputs        
        if self._errors:
            mapping['errors'] = self._errors        
        if self._warnings:
            mapping['warnings'] = self._warnings        
        return mapping


    def _file(self, *args):
        if args:
            # Note: str(args[0]) handles if the first arg is already a path string
            result_file = op.join(self._prefix + str(args[0]), *map(str, args[1:]))
        else:
            result_file = self._prefix
            
        if self._create:
            os.makedirs(op.dirname(result_file) or '.', exist_ok=True)
        return result_file


    def _folder(self, *args):
        return op.dirname(self._file(*args) or '.')


    def _append_error(self, msg):
        self._errors.append(msg)


    def _append_warning(self, msg):
        self._warnings.append(msg)


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
    def __init__(self,logFile,jobName='Job',resultFolderOrPrefix=None,output_patterns=None,shell_context={}):
        if isinstance(logFile,str):
            self.logFile = logFile
        else:
            # This makes it possible in a Snakefile to use JobMonitor(log)
            self.logFile = logFile[0]
        self.jobName = jobName
        self.markdown = self.logFile.endswith('.md')
        self.shell_context = shell_context

        # fullPrefix is the combination of resultFolder and resultPrefix
        if not resultFolderOrPrefix:
            resultFolderOrPrefix = op.dirname(self.logFile)
        if resultFolderOrPrefix.endswith('*'):
            prefix = resultFolderOrPrefix[:-1]
        else:
            prefix = op.join(resultFolderOrPrefix,'')
            
        self.result = JobResult(self.logFile,prefix=prefix,create=True,output_patterns=output_patterns)
        
        # allow JobMonitor to be run out-of-context
        self.started = datetime.now()
        self.runningLog = None
        
        # works with tmpdir function
        self._tmpdir = None
            
            
    def tmpdir(self,*args):
        if self._tmpdir is None:
            self._tmpdir = tempfile.TemporaryDirectory()
        
        path = self._tmpdir.name
        if len(args):
            path = op.join(path,*args)
            os.makedirs(path,exist_ok=True)
        return path


    def tmpfile(self,name):
        return op.join(self.tmpdir(),name)


    def __enter__(self):
        self.started = datetime.now()
        logFolder = op.dirname(self.logFile) or '.'
        os.makedirs(logFolder,exist_ok=True)
            
        self.runningLog = replaceInnerExtension(self.logFile,'.running')
        with open(self.runningLog,'wt') as fp:
            fp.write(f'"{self.jobName}" started at {self.started}, saving output to\n{self.result()}\n')
        
        try:
            # if process failed previously, remove the failure report
            oldFail = replaceInnerExtension(self.runningLog,'.error')
            if op.exists(oldFail):
                 os.remove(oldFail)
            
        except Exception as e:
            tb = traceback.format_exc().splitlines()
            raise RuntimeError(f'Error in JobMonitor, {tb}')
            
        return self


    def __exit__(self, exc_type, exc_value, tb):
        self.stopped = datetime.now()
        elapsed = self.stopped-self.started
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

        with open(self.runningLog,'at') as fp:
            fp.write('Output mapping\n')
            json.dump(self.result._output_mapping(),fp,indent=2)
            
        os.rename(self.runningLog,self.logFile)
        self.runningLog = self.logFile
        
        if self._tmpdir:
            self._tmpdir.cleanup()    
            self._tmpdir = None
        return True

            
    # return error message, if any
    def checkError(self,logFile=None):
        if logFile is None:
            logFile = self.logFile
        if not isinstance(logFile,str):
            logFile = logFile[0]
        errorFile = replaceInnerExtension(logFile,'.error')
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
        with open(self.runningLog,'at') as fp:
            if timeIt:
                elapsed = datetime.now()-self.started
                fp.write(f'[{elapsed}] {msg}\n')
            else:
                fp.write(f'{msg}\n')


    def error(self, msg):
        if isinstance(msg, BaseException):
            tb = traceback.format_exc()
            msg = f"{msg}\n{tb}"
        else:
            msg = str(msg)

        self.log(f"Error: {msg}", timeIt=False)

        # create an error message file
        errorFile = replaceInnerExtension(self.logFile,'.error')
        with open(errorFile,'at') as fp:
            fp.write(msg+'\n')


    def periodic_log(self,p_stdout,interval,formatter):
        lines = []
        def flush():
            if formatter is None:
                formatted = '\n'.join(lines)
            else:
                formatted = formatter(lines)

            if formatted:
                self.log(formatted, timeIt=False)

            lines.clear()        
            return formatted
                    
        last_flush = time.time()
        output = []
        for line in iter(p_stdout.readline, ''):
            lines.append(line.rstrip('\n'))

            if (time.time()-last_flush >= interval):
                output.append( flush() )
                last_flush = time.time()
                    
        if len(lines):
            output.append( flush() )
            
        return '\n'.join(output)
      

    def run(self, cmd, cwd=None, timeout=None, env=None,
            failOnError=True, updateInterval_s=None, formatter=None):

        msg = f'Running process `{subprocess.list2cmdline(cmd)}`'
        print(f'{msg},\n=> output to {self.logFile}.')
        self.log(msg)

        if self.markdown:
            self.log('```\n')

        if updateInterval_s is not None:
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
                env=env,
                bufsize=1
            )

            output = self.periodic_log(p.stdout, updateInterval_s, formatter)
            p.wait()
        else:
            p = subprocess.run(
                cmd,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_output=True,
                text=True
            )

            # Log stdout
            output = (p.stdout or "") + (p.stderr or "")
            if output:
                self.log(output + ("" if output.endswith("\n") else "\n"))

        if self.markdown:
            self.log('```\n')

        returnCode = p.returncode
        if returnCode > 0:
            error = output if len(output)<=1000 else '...\n'+output[-1000:]
            if failOnError:
                raise RuntimeError(error)
            else:
                self.error(error)

        return returnCode, output
                
    
    def shell(self,script):
        from snakemake import shell as sh
        sh(f'set +euo pipefail; exec >> {shlex.quote(self.runningLog)} 2>&1; '+script,**self.shell_context)
