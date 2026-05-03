import time
import yaml
import sys

try:
    from yaml import CSafeLoader as FastLoader
    print("CSafeLoader available")
except ImportError:
    from yaml import SafeLoader as FastLoader
    print("CSafeLoader NOT available")

from yaml import SafeLoader as SlowLoader

test_yaml = """
title: Test
tags: [a, b, c, d]
date: 2024-05-03
id: 20240503_123456
author: User
description: "A longer description of the test item that takes a bit more parsing. "
"""

n = 10000

t0 = time.time()
for _ in range(n):
    yaml.safe_load(test_yaml)
t1 = time.time()
print(f"safe_load (pure python): {t1 - t0:.4f}s")

t0 = time.time()
for _ in range(n):
    yaml.load(test_yaml, Loader=FastLoader)
t1 = time.time()
print(f"load with FastLoader: {t1 - t0:.4f}s")
