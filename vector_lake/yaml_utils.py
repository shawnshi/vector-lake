import yaml
from yaml.error import YAMLError

try:
    from yaml import CSafeLoader as SafeLoader, CDumper as Dumper
except ImportError:
    from yaml import SafeLoader, Dumper

def load_yaml(stream):
    """
    Parse the first YAML document in a stream and produce the corresponding Python object.
    Uses LibYAML C extension if available for up to 7x performance improvement.
    """
    return yaml.load(stream, Loader=SafeLoader)

def dump_yaml(data, stream=None, **kwds):
    """
    Serialize a Python object into a YAML stream.
    Uses LibYAML C extension if available for up to 4x performance improvement.
    """
    return yaml.dump(data, stream, Dumper=Dumper, **kwds)
