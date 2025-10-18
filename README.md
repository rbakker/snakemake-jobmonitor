# snakemake-jobmonitor package
`snakemake-jobmonitor` is an alternative take on the regular [Snakemake](https://snakemake.readthedocs.io/en/stable/) workflow. Instead of passing input and output-files around, it passes log-files around. The log-files contain pointers to result-files. The advantage of this is much better progress monitoring, error handling and logging. The JobMonitor and JobResult classes ensure that this can be achieved with minimal code that is easy to read and maintain. `snakemake-jobmonitor` is a super minimal library of just two pages, it does not modify Snakemake, only the way Snakemake is used.

## Regular Snakemake

Snakemake is a powerful workflow-engine that compiles *rules* into a DAG (Directed Acyclic Graph) and automatically determines a parallel execution strategy.
Rules invoke each other via *filenames*, which typically contain wildcards so that the same rule can be invoked for multiple cases.

### Usage example

Example of a Snakemake file that splits a color image into the red, green and blue component for cases '1','2' and '3'. 

```python
inputFolder = '/path/to/cases'
outputFolder = '/path/to/results'
allCases = ['1','2','3']

def doDecompose(colorInfile, redOutfile,greenOutfile,blueOutfile):
    # implement here:
    # Decompose the color image into red, green and blue components
    # and save as redOutfile, blueOutfile and greenOutfile

def createReport(allRed,allBlue,allGreen, reportFile):
    # implement here:
    # Create report and save as reportFile


rule decomposeSingle:
    input:
        color='inputFolder/case-{case}_RGB.png'
    output:
        R='outputFolder/case-{case}_R.png',
        G='outputFolder/case-{case}_G.png',
        B='outputFolder/case-{case}_B.png'
    run:
        doDecompose(input.color, output.R,output.G,output.B)


rule decomposeAll:
    input:
        R=[f'{outputFolder}/case-{cs}_R.png' for cs in allCases],
        G=[f'{outputFolder}/case-{cs}_G.png' for cs in allCases],
        B=[f'{outputFolder}/case-{cs}_B.png' for cs in allCases]
    output:
        report=f'{outputFolder}/report.txt'
    default_target:
        True
    run:
        createReport(input.R,input.G,input.B, output.report)
```

### Practical issues

For larger workflows some issues arise:

1. Snakemake does not come with a good progress monitor. There is a possibility to use the 'WMS monitoring protocol' but this is cumbersome to setup and being phased out. It is replaced by 'logger plugins', but these are still experimental.

2. If an error occurs, the pipeline stops and produces a very long error trace, most of which is irrelevant to the error. Or one can opt to ignore errors, but this will cause errors down the line that are even more difficult to trace.

3. Snakemake produces a log-file that contains information about process execution, but does not contain the console-output of the processes called by each rule. This is because a global log file is not suitable to contain logs from different components that may run in parallel.

4. If a rule has many outputs, and another rule needs these as inputs, the rules become cluttered.  

### An alternative approach

To solve these issues, `snakemake-jobmonitor` changes the way rules interact. Every rule is producing a **log-file** instead of **output files**. And instead of having rule B request the *output* of rule A, it requests the *log-file* of rule A. Inside that log-file there is a pointer to where the rule results are stored. 

Snakemake-jobmonitor is implemented as a class that acts as a context-manager. A typical rule looks as follows:

```python
rule decomposeSingle:
    input:
        color='inputFolder/case-{case}_RGB.png'
    log:
        'logFolder/case-{case}_decompose.log'
    run:
        caseFolder = f'{outputFolder}/case-{wildcards.case}'
        with JobMonitor(log,'Decompose RGB into R,G,B',caseFolder) as job:
            doDecompose(input.color, 
                job.result('R.png'),job.result('G.png'),job.result('B.png'))
```

The rule has changed in a few places, instead of producing three output files it produces a log file. In the statement that starts with `with JobMonitor`, JobMonitor creates the log file and in that file it stores the path where the rule output will be written, in this case in the `caseFolder` folder.  In the last line, the `job.result('R.png')` creates the output folder and returns the full path to the file.

Although the code has become two lines longer, it offers huge advantages:

1. JobMonitor automatically creates the .log file, but while the rule executes the extension is changed into '.running'. So, at any moment you can see what Snakemake is working on by listing all *.running* files In the log folder.

2. If an error occurs within the JobMonitor context, the error is appended to the log file and written to a *.error* file (with otherwise the same name as the *.log* file). So, one can easily find all rules that gave errors by listing *.error* files in the log folder.  After fixing the code that produced the error, delete the corresponding log-file before re-running Snakemake.

3. Naturally, every rule produces its own log. In addition, JobMonitor provides a `run` method to invoke external software. This method is mostly the same as `subprocess.run`, but it captures all output to the *.log* file and sends errors to the *.error* file.

4. Rules have inputs that are log files produced by other rules. And a single output: its own log file. The Snakefile is not cluttered by declaring all the output files that may be produced by each rule. Those are accessed indirectly via the result-pointer in its log-file. 


## Full snakemake-jobmonitor example

Here is the full version of the previous example in the `snakemake-jobmonitor` style.

```python
from snakemake-jobmonitor import JobMonitor, JobResult

inputFolder = '/path/to/subjects'
logFolder = '/path/to/logs'
outputFolder = '/path/to/results'

allCases = ['1','2','3']

def doDecompose(colorInfile, redOutfile,greenOutfile,blueOutfile):
    # implement here:
    # Decompose the color image into red, green and blue components
    # and save as redOutfile, blueOutfile and greenOutfile

def createReport(allRed,allBlue,allGreen, reportFile):
    # implement here:
    # Create report and save as reportFile


rule decomposeSingle:
    input:
        color='inputFolder/case-{case}_RGB.png'
    log:
        'logFolder/case-{case}_decompose.log'
    run:
        caseFolder = f'{outputFolder}/case-{wildcards.case}'
        with JobMonitor(log,'Decompose RGB into R,G,B',caseFolder) as job:
            doDecompose(
                input.color, 
                job.result('R.png'),job.result('G.png'),job.result('B.png')
            )


rule decomposeAll:
    input:
        [f'{logFolder}/case-{cs}_decompose.log' for cs in allCases]
    log:
        f'{logFolder}/decomposeAll.log'
    default_target:
        True
    run:
        with JobMonitor(log,'Decompose All',outputFolder) as job:
            R = [JobResult(f)('R.png') for f in input]
            G = [JobResult(f)('G.png') for f in input]
            B = [JobResult(f)('B.png') for f in input]
            createReport(R,G,B, job.result('report.txt') )
```



## Usage of JobMonitor

Signature: `JobMonitor(logFile,description,resultFolder)`

The JobMonitor class takes three arguments:

- `logFile` path to the log file. If the file exists, it will be overwritten.

- `description` brief description of what the rule does.

- `resultFolder` path the result folder. One can also pass a result prefix by adding an asterisc at the end. Examples:
  
  - `/my/results/case-1` will cause results to be written in the `case-1` folder.
  
  - `/my/results/case-1_*` will cause results to be written in the results folder, and every file therein will start with `case-1_`.

JobMonitor should be used as a context manager, like

```python
with JobMonitor(logFile,description,resultFolder) as job:
    doSomething()
```

Inside the context, `job` can be used for the following tasks:

1. Create/access the result of this rule via `job.result(resultFile)`
   
   This returns a filename that concatenates the previously specified `resultFolder` with `resultFile`, and will make sure the folder is created. One can also write results in subfolders, by just adding arguments, like `job.result(subFolder,resultFile)`.  Examples:
   
   * If the resultFolder is specified as `/my/results/case-1`, then `job.result('test','R.png')` will return `/my/results/case-1/test/R.png`.
   
   * If the resultFolder is specified as `/my/results/case-1_*`, then `job.result('test','R.png')` will return `/my/results/case-1_test/R.png`
   
   This is all that `job.result` is doing, it just returns filenames and creates folders, it does not create results, that is up to the code inside the rule.

2. Run an external command via `job.run(command,liveUpdates=False)`.
   
   Here command must NOT be a string, but rather a list of strings that follows the exact same rules as the [subprocess.run](https://docs.python.org/3/library/subprocess.html#subprocess.run) command. The advantage of using `job.run()` is that it saves `stdout` and `stderror` to the log/error file respectively. 
   
   If `liveUpdates` is set to `False`, then the log/error file will be updated once the command is finished, if set to `True` the update is more frequent.

## Usage of JobResult

Signature: `JobResult(logFile)`

We already used `job.result` in the previous chapter to access result files inside the `JobMonitor` context. The `JobResult` class is to access results of other rules that produced log-files. It makes use of the fact that every log-file contains, on the second line, the `resultFolder` of the rule that created it.

If we start for example with:

```python
result = JobResult('/my/logfolder/case-1_test.log')
```

then `result` can be used in the same way as `job.result` in the previous chapter. For example, `result(subFolder,resultFile)` will return the concatenation of resultFolder, subFolder and resultFile. It will not create any folders, that only happens in the JobMonitor context.

`JobResult` has some additional convenience methods:  

* `result.file(*args)` is the same as `result(*args)`

* `result.folder(*args)` returns the result folder, internally using `os.path.dirname(resultFile)`

* `result.parseJson(*args)` parses the json-formatted result file and returns its content.


