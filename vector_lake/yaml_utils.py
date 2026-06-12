import yaml

try:
    from yaml import CSafeLoader as SafeLoader, CSafeDumper as Dumper
except ImportError:
    from yaml import SafeLoader, SafeDumper as Dumper


def load_yaml(stream):
    """
    Load YAML safely and efficiently.
    Uses LibYAML C extensions if available, falling back to pure Python implementation.
    """
    return yaml.load(stream, Loader=SafeLoader)


def dump_yaml(data, **kwargs):
    """
    Dump YAML efficiently.
    Uses LibYAML C extensions if available, falling back to pure Python implementation.
    """
    return yaml.dump(data, Dumper=Dumper, **kwargs)
