import time
import yaml
from yaml import SafeLoader
try:
    from yaml import CSafeLoader
except ImportError:
    CSafeLoader = None

data = """
id: 20240101_abcdef
title: "Test Node"
type: concept
tags:
  - one
  - two
  - three
categories:
  - test
"""

print("Benchmarking pure Python SafeLoader vs CSafeLoader")

# Pure Python
start = time.time()
for _ in range(10000):
    yaml.load(data, Loader=SafeLoader)
end = time.time()
print(f"Pure Python SafeLoader: {end - start:.4f}s")

if CSafeLoader:
    start = time.time()
    for _ in range(10000):
        yaml.load(data, Loader=CSafeLoader)
    end = time.time()
    print(f"CSafeLoader: {end - start:.4f}s")
else:
    print("CSafeLoader not available")
