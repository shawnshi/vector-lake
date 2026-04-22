import os

files = [
    'MEMORY/raw/article/医疗数字化三十讲/第二十五讲：结构化思维与问题诊断：咨询顾问的“手术刀”.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十八讲：数据分析与价值呈现：让数据“说话”的艺术.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十六讲：蓝图规划与方案设计：从“看病”到“开方”.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十四讲：数字疗法（DTx）与 RWE：超越医院围墙的新范式.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md'
]
root = '../../'

for f in files:
    path = os.path.join(root, f)
    print(f'=== FILE: {f} ===')
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as s:
                print(s.read())
        else:
            print(f'File not found at {path}')
    except Exception as e:
        print(f'Error reading {path}: {e}')
    print('=== END ===')
