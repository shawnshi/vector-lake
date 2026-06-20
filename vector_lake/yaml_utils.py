import yaml
try:
    # ⚡ Bolt Optimization: Use LibYAML C-extensions for ~6-7x faster parsing/dumping
    from yaml import CSafeLoader as SafeLoader, CSafeDumper as SafeDumper
except ImportError:
    # Fallback for environments lacking C extensions
    from yaml import SafeLoader, SafeDumper

def load_yaml(stream):
    """Loads YAML using the fastest available safe loader."""
    return yaml.load(stream, Loader=SafeLoader)

def dump_yaml(data, stream=None, **kwds):
    """Dumps YAML using the fastest available safe dumper."""
    return yaml.dump(data, stream, Dumper=SafeDumper, **kwds)
