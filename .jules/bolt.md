## 2024-05-18 - [Fast CLoader support in Python for yaml module]
**Learning:** For optimal YAML parsing and dumping performance, use `yaml.load(data, Loader=SafeLoader)` and `yaml.dump(data, Dumper=Dumper)` after attempting to import `CSafeLoader as SafeLoader, CDumper as Dumper` from `yaml` with a fallback to the pure Python `SafeLoader` and `Dumper`. This ensures performance speedups without risking portability issues in environments lacking LibYAML.
**Action:** Always attempt to import C versions of `SafeLoader` and `Dumper` from `yaml` in files dealing with yaml.
