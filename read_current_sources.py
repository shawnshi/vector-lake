import os

files = [
    'raw/article/医疗数字化三十讲/第二部分：战场解析篇——深入大型医院的业务腹地与数字化实践.md',
    'raw/article/医疗数字化三十讲/第五讲：他山之石：全球视野下的医疗数字化模式对比与启示.md',
    'raw/article/医疗数字化三十讲/第八讲：医生的“眼睛”和“画笔”：PACS 与 LIS 系统详解.md',
    'raw/article/医疗数字化三十讲/第六讲：心脏与大脑（上）：以电子病历（EMR）为核心的临床数据体系.md',
    'raw/article/医疗数字化三十讲/第十一讲：从“住院”到“住店”：打造无缝衔接的住院服务体验.md'
]
root = '../../memory'

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
