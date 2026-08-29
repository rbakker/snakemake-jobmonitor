import os
from os import path as op
import json
from itertools import zip_longest
import string
from shlex import quote

from typing import Any, Dict, Tuple, Type, Union
from types import SimpleNamespace, FunctionType
from pydantic import BaseModel, Field, ConfigDict
from pydantic_core import PydanticUndefined

from jobmonitor import JobMonitor, JobResult


class Fixed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    

class IgnoreExtra(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AllowExtra(BaseModel):
    model_config = ConfigDict(extra="allow")


class PartialFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            if key in kwargs:
                return kwargs[key]
            # leave unknown fields untouched
            return "{" + key + "}"
        return string.Formatter.get_value(self, key, args, kwargs)

partial_formatter = PartialFormatter()


class SnakeRule:
    name = "[no name]" # name of this rule, may vary per instance
    description = "[no description]" # description of what this rule does
    log_root = '.'
    result_root = '.'
    
    class WildcardModel(BaseModel): # describes wildcards and how they appear in file names
        pass
    class InputModel(BaseModel): # describes expected inputs            
        pass
    class OutputModel(BaseModel): # describes outputs + default file names
        pass        
    class ParamModel(BaseModel): # describes parameters + defaults
        pass

    threads = 8
    conda_env = None
    default_target = False
    is_checkpoint = False


    def __init__(self, params=None):
        snake = snakepipeline()
        self.params = params # params are configured later
        self.inputs = {}


    def set_input(self, **inputs):
        self.inputs = self.InputModel(**inputs)  # validate keys/shape; result discarded on purpose
        return self


    # Only to be used as argument to set_input() of another SnakeRule, see resolved_inputs().
    def get_output(self, field=None, parser=None):
        if field is None:
            # return complete output
            return self
        # return specific output
        return OutputPromise(self, field, parser)


    def configure(self,name,snake_pipeline):
        if isinstance(self.params, ParamModel):
             raise RuntimeError('Parameters of rule {name} are already configured.')
        self.params = ParamModel(**(self.params or snake_pipeline.rule_params(self.__class__)))      

    
    def foreach(self, *wildcard_list_of_dicts, **wildcard_dict_of_lists):
        return JobLoop(self, *wildcard_list_of_dicts, **wildcard_dict_of_lists)


    # Snakemake will ensure that run_job is called after all input files have been created.
    def run_job(self, rule_context):
        wildcards = rule_context['wildcards']
        inputs = self.resolved_inputs(wildcards)
        params = self.params
        log_path = self.log_path(wildcards)
        descr = self.descr(wildcards)
        result_path = self.result_path(wildcards)

        output_patterns = {
            key: partial_formatter.format(field.default, params=params, **wildcards)
            for key, field in OutputModel.model_fields.items()
            if isinstance(field.default, str)
        }

        with JobMonitor(log_path, descr, result_path, output_patterns=output_patterns, shell_context=rule_context) as job:
            self.run(job, inputs, job.result, params, wildcards)


    def resolve(self, wildcards, field, parser):
        val = JobResult(self.log_path(wildcards))
        try:
            val = getattr(val, field) if field else val
        except Exception:
            val = None
        return parser(val, wildcards) if parser else val


    def resolved_inputs(self, wildcards):
        inp = {}
        for key, val in self.inputs.items():
            if isinstance(val, str):
                # simple filename input
                inp[key] = partial_formatter.format(val, **wildcards)
            elif isinstance(val, (list, tuple)):
                inp[key] = [partial_formatter.format(v, **wildcards) for v in val]
            elif isinstance(val, FunctionType):
                inp[key] = val(wildcards)
            else:
                rule_or_loop = val
                field = None
                parser = None
                if isinstance(val, OutputPromise):
                    rule_or_loop = val.rule_or_loop
                    field = val.field
                    parser = val.parser

                inp[key] = rule_or_loop.resolve(wildcards, field, parser)

        return SimpleNamespace(**inp)


    def descr(self, wildcards):
        if self.wildcard_fields:
            d = dict(wildcards)
            kvpairs = ','.join(f"{k}={v}" for k, v in d.items())
            return f"{self.name}({type(self)})<{kvpairs}>"
        return self.name


    def _input_paths(self, snake_checkpoint_magic):
        # snake_checkpoint_magic makes the checkpoints variable available

        paths = []
        for key, val in self.inputs.items():
            if isinstance(val, str):
                # simple filename input
                paths.append(val)
            elif isinstance(val, (list, tuple)):
                # multiple filename input
                paths.append(val)
            elif isinstance(val, FunctionType):
                # input function that returns paths, account for checkpoints here
                paths.append(snake_checkpoint_magic(val))
            else:
                # input via one or more SnakeRule objects
                rule_or_loop = val
                if isinstance(val, OutputPromise):
                    rule_or_loop = val.rule_or_loop

                # account for checkpoints here
                paths.append(snake_checkpoint_magic(rule_or_loop.log_path))

        return paths


    def log_template(self):
        print(list(WildcardModel.model_fields.items()))
        parts = [field.default.format('{' + key + '}') for key, field in WildcardModel.model_fields.items()]
        print(parts,self.name)
        parts.append(self.name)
        return op.join(self.log_root, '_'.join(parts) + ".log")


    def log_path(self, wildcards=None):
        fmt = self.log_template()
        return fmt if wildcards is None else partial_formatter.format(fmt, **wildcards)


    def result_template(self):
        parts = [field.default.format('{' + key + '}') for key, field in WildcardModel.model_fields.items()]
        parts.append(self.name)
        return op.join(self.result_root, *parts)


    def result_path(self, wildcards=None):
        fmt = self.result_template()
        return fmt if wildcards is None else fmt.format(**dict(wildcards))


    def run(self, job, input, output, params, wildcards):
        raise NotImplementedError


    def summarize(self, job, input, output, params):
        # Purpose is to collect input, not to generate output
        print('Job', self.name, 'finished.')
        
        
    def as_snake(self, snake_checkpoint_magic):
        return SimpleNamespace(
            name=self.name,
            input=self._input_paths(snake_checkpoint_magic),
            params=self.params,
            log=self.log_path(),
            default_target=self.default_target,
            conda=self.conda_env,
            run_job=self.run_job,
            is_checkpoint=self.is_checkpoint,
        )

    @classmethod
    def describe(cls):
        print(f"{cls.__name__}: {cls.description}")
        for title, model in [('wildcards', WildcardModel), ('inputs', InputModel),
                              ('outputs', OutputModel), ('params', ParamModel)]:
            print(f"{cls.__name__} {title}:")
            for key, field in model.model_fields.items():
                default = '' if field.default is PydanticUndefined else f" (default={field.default!r})"
                print(f"  {key}: {field.description}{default}")        


class OutputPromise:
    def __init__(self,rule_or_loop,field,parser):
        self.rule_or_loop = rule_or_loop
        self.field = field
        self.parser = parser
  
        
class JobLoop:
    def __init__(self,rule,*wildcard_list_of_dicts,**wildcard_dict_of_lists):
        self.rule = rule
        self.scalar = True
        self.wildcard_iterables = []
        self.init(*wildcard_list_of_dicts,**wildcard_dict_of_lists)
    
    
    # Use same get_output as SnakeRule
    get_output = SnakeRule.get_output


    def init(self,*wildcard_list_of_dicts,**wildcard_dict_of_lists):
        """
        Use wildcard_list_of_dicts to define a group of wildcards.
        - Each element yields a dict containing wildcard name/value pairs.
        
        Use wildcard_dict_of_lists as an alternative or supplemental method.
        - Each item contains value(s) for a single wildcard name.
        - The value can be a single string or array of strings.
        """
        self.scalar = True
        self.wildcard_iterables = []
        if wildcard_list_of_dicts:
            self.scalar = False
            self.wildcard_iterables.extend(wildcard_list_of_dicts)

        if wildcard_dict_of_lists:
            def my_wildcard_iterator(parent_wildcards):
                counts = {k: len(v) for k, v in wildcard_dict_of_lists.items()
                          if isinstance(v, (list, tuple))}
                if len(counts):
                    self.scalar = False
                longest = max(counts.values(), default=1)

                for i in range(longest):
                    yield {k: (v[i] if k in counts else v)
                           for k, v in wildcard_dict_of_lists.items()}
                
            self.wildcard_iterables.append(my_wildcard_iterator)
            

    def wildcard_iterator(self, parent_wildcards):
        iterables = [
            it(parent_wildcards) if callable(it) else iter(it)
            for it in self.wildcard_iterables
        ]

        first = None

        for dicts in zip_longest(*iterables, fillvalue=None):
            if first is None:
                first = dicts

            merged = dict(parent_wildcards)

            for i, d in enumerate(dicts):
                merged.update(d or first[i])

            yield merged
            
            
    def resolve(self,wildcards,field,parser):
        val = [ JobResult( self.rule.log_path(wc) ) for wc in self.wildcard_iterator(wildcards) ]
        if field:
            val = [ getattr(r,field) for r in val ]
        val = val[0] if self.scalar else val
        return parser(val,wildcards) if parser else val
        
    # The log_path of a loop contains a list with all log_paths of its members.  
    # wildcard_iterator may depend on a checkpoint.
    def log_path(self,parent_wildcards=None):
        val = [ self.rule.log_path(wc) for wc in self.wildcard_iterator(parent_wildcards) ]
        return val[0] if self.scalar else val


class SnakeCheckpoint(SnakeRule):
    is_checkpoint = True


class SnakePipeline:
    class PathVars(Fixed):
        logs: str
        results: str
        resources: str


    class ConfigModel(Fixed):
        pathvars: PathVars = Field(description="Path variables"),
        env: dict[str, str] = Field(description="Default environment variables")),
        params: dict[str, dict] = Field(description="Default rule parameters, matching the 'param_fields' definition of each rule"))


    def __init__(self, output_root, config):
        extended_config = dict(
            params=config.get('params', {}),
            env=config.get('env', {}),
            pathvars=dict(
                logs=op.join(output_root, 'logs'),
                results=op.join(output_root, 'results'),
                resources=op.join(output_root, 'resources'),
            ),
        )
        self.config = self.ConfigModel(**extended_config)


    @staticmethod
    def _key(cls_or_name):
        return cls_or_name if isinstance(cls_or_name, str) else cls_or_name.__name__


    #def rule(self, Rule, **params):
    #    key = self._key(Rule)
    #    if key in self.config.params:
    #        unknown = set(self.config.params[key]) - set(Rule.ParamModel.model_fields)
    #        if unknown:
    #            raise ValueError(f"Snakemake config: unknown/misspelled parameters for rule '{key}': {unknown}")
    #    defaults = self.config.params.get(key, {})
    #    merged = {**defaults, **params}
    #    rule = Rule(self.config, merged)
    #    self._rules.append(rule)
    #    return rule
    
    def config_params(cls):
        return self.config.params.get(self._key(cls), {})
        # CONTINUE HERE BY CHECKING WITH Rule.ParamModel OR is that done later?
        
        
    def target(self,rule_or_loop):
        class All(SnakeRule):
            class InputModel(Fixed):
                target: Any = Field(description='Catch-all input'))
                
            default_target = True
            def run(self,job,input,output,params,wildcards):
                print('All jobs done.')

        self.rule(All).set_input(
            target = rule_or_loop
        )
                    

    # Collect all rules in the scope of namespace.
    # And give the rules a unique name.
    @staticmethod
    def collect_rules(namespace):
        passed = {}
        for name, obj in namespace.items():
            if isinstance(obj, SnakeRule):
                obj.name = name
                obj.configure(name,self.pathvars,self.params)
                passed[name] = obj
                
        return passed


    def build(self,namespace,inject_rule,checkpoint_magic,verbose=False):
        """
        Build the snakemake pipeline from all rules present in namespace (typically locals() in snakefile) 
        """
        rules_available = self.collect_rules(namespace)
        
        # find target rule, try to find default_target, otherwise use first rule that has no wildcards
        first_target = None
        default_target = None
        for c in rules_available.values():
            if len(c.wildcard_fields) == 0:
                first_target = c
            if c.default_target:
                default_target = c
        if not default_target:
            default_target = first_target
        if not default_target:
            raise RuntimeError(f'Could not figure out which of the {len(rules_available)} rules to run. Specify default_target.')
        
        print('Target rule:',default_target,default_target.wildcard_fields)

        # inject all available rules
        for rule in rules_available.values():
            r = rule.as_snake(checkpoint_magic)
            if verbose:
                print('Injecting rule\n',json.dumps(vars(r),indent=2,default=str))
            inject_rule(r)


def create_documentation(snake_namespace, output_path):
    active_elements = collect_rules(snake_namespace)
    
    # 1. Build Mermaid String
    mermaid_code = [
        "---",
        "config:",
        "  theme: 'base'",
        "  themeVariables:",
        "    primaryColor: '#eee'",
        "    primaryTextColor: '#000'",
        "    primaryBorderColor: '#bbb'",
        "    lineColor: '#000'",
        "    secondaryColor: '#ffe'",
        "    tertiaryColor: '#fff'",
        "---",
        "graph LR"
    ]
    details_html = []

    for name, obj in active_elements.items():
        is_loop = isinstance(obj, JobLoop)
        rule = obj.rule if is_loop else obj
        cls = rule.__class__
        
        # Node setup
        node_id = f"node_{id(obj)}"
        
        # 1. Get the wildcard names for this rule
        # Assuming your Rule classes have a 'wildcards' attribute or similar
        # If they are derived from the JobLoop, we can pull them there
        w_keys = rule.wildcard_fields.keys()
        w_text = f"<font color='blue'>{'/'.join(w_keys)}</font>" if w_keys else ""
        
        # 2. Build the multi-line label with a separating line (<hr/>)
        # Using a <div> wrapper to ensure clean alignment
        node_label = (
            f'<div style="line-height: 1.4; width: 100%; border-bottom: 1px solid black">'
            f'<b>{name}</b></div>'
            #f'<hr style="border: 0; border-top: 1px solid #ccc; margin: 4px 0;"/>'
            f'{cls.__name__}'
            f'<br/><small>{w_text}</small>'
        )
        
        mermaid_code.append(f'    {node_id}["{node_label}"]')
        
        # Arrow logic
        for key, val in obj.inputs.items():
            inp_rule = None
            inp_loop = None
            
            # Identify the source
            if isinstance(val, (SnakeRule, JobLoop)):
                inp_rule = val
            elif isinstance(val, OutputPromise):
                inp_rule = val.rule_or_loop
            
            # Distinguish between direct rule and loop wrapper
            if isinstance(inp_rule, JobLoop):
                inp_loop = inp_rule
                inp_rule = inp_loop.rule  # This is the actual rule generating data
            
            if inp_rule:
                source_id = f'node_{id(inp_rule)}'
                
                if inp_loop:
                    # 1. Create a unique Aggregator Node ID for this specific connection
                    agg_id = f'agg_{id(inp_loop)}_{id(obj)}_{key}'
                    
                    # 2. Extract loop metadata
                    w_it = inp_loop.wildcard_iterator({})
                    try:
                        first = next(w_it)
                        w = '/'.join(first.keys())
                        count = 1 + sum(1 for _ in w_it)
                    except StopIteration:
                        w, count = "empty", 0
                    
                    # 3. Create the Aggregator Node (Double circle or Diamond look)
                    # Using (( )) for a circular "node" to represent the loop operation
                    loop_icon = "&#8635;" 
                    agg_label = f'<span style="font-size: 2em; line-height: 0.8">{loop_icon}</span><br/><font color="blue"><small>{w}<br/>{count} items</small></font>'
                    mermaid_code.append(f'    {agg_id}(("{agg_label}")):::loopStyle')
                    
                    # 4. Connections: Rule -> Aggregator -> Target
                    out_label = f'|{key}|' if key != '_' else ''
                    # Connection A: Rule to Aggregator (Standard line + Output name)
                    mermaid_code.append(f'    {source_id} -->{out_label} {agg_id}')
                    # Connection B: Aggregator to Target (Double line to show aggregation)
                    mermaid_code.append(f'    {agg_id} ==> {node_id}')
                
                else:
                    # Standard Rule-to-Rule connection
                    field_label = f'|{key}|' if key != '_' else ''
                    mermaid_code.append(f'    {source_id} -->{field_label} {node_id}')                

        # 2. Build Technical Reference HTML
        details_html.append(f"""
            <div class="rule-card">
                <h3>{name} <small>({cls.__name__})</small></h3>
                <table>
                    <tr><th>Field</th><th>Type</th><th>Description</th></tr>
                    {"".join([f"<tr><td>{fn}</td><td>Input</td><td>{fi.description or ''}</td></tr>" for fn, fi in cls.input_fields.items()])}
                    {"".join([f"<tr><td>{fn}</td><td>Output</td><td>{fi.description or ''} (Default: {fi.default})</td></tr>" for fn, fi in cls.output_fields.items()])}
                </table>
            </div>
        """)

    # 3. Combine into a Single HTML File
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
        <script>mermaid.initialize({{startOnLoad:true}});</script>
        <style>
            body {{ font-family: sans-serif; margin: 40px; line-height: 1.6; color: #333; }}
            .rule-card {{ border: 1px solid #ddd; padding: 15px; margin-bottom: 20px; border-radius: 8px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #eee; }}
            th {{ background-color: #f4f4f4; }}
            h3 {{ margin-top: 0; color: #2c3e50; }}
            .edgeLabel div {{
                background-color: #ffe !important;
                border: 1px solid #bbb !important;
                border-radius: 0.5ex;
                padding: 2px 0.5ex !important;
                font-size: 0.9em;
            }}
        </style>
    </head>
    <body>
        <h1>Pipeline Documentation</h1>
        <div class="mermaid">
            {"\n".join(mermaid_code)}
        </div>
        <h2>Rule Details</h2>
        {"".join(details_html)}
    </body>
    </html>
    """

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html_template)
    
    print(f"Documentation generated at: {output_path}")


