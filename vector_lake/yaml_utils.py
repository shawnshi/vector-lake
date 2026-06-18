import yaml

try:
    from yaml import CSafeLoader as SafeLoader, CSafeDumper as Dumper
except ImportError:
    from yaml import SafeLoader, SafeDumper as Dumper

def load_yaml(stream):
    return yaml.load(stream, Loader=SafeLoader)

def dump_yaml(data, stream=None, **kwargs):
    return yaml.dump(data, stream, Dumper=Dumper, **kwargs)
