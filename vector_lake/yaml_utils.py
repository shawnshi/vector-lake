import yaml

try:
    from yaml import CSafeLoader as SafeLoader, CSafeDumper as Dumper
except ImportError:
    from yaml import SafeLoader, SafeDumper as Dumper

def load_yaml(data):
    return yaml.load(data, Loader=SafeLoader)

def dump_yaml(data, **kwargs):
    return yaml.dump(data, Dumper=Dumper, **kwargs)
