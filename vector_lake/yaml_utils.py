import yaml

try:
    from yaml import CSafeLoader as SafeLoader, CDumper as Dumper
except ImportError:
    from yaml import SafeLoader, Dumper

def load_yaml(stream):
    return yaml.load(stream, Loader=SafeLoader)

def dump_yaml(data, **kwargs):
    return yaml.dump(data, Dumper=Dumper, **kwargs)
