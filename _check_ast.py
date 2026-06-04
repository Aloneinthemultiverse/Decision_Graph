from decisiongraph.codebase_ast import parse_file_full
code = open(r'C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench\src\flask\app.py', encoding='utf-8').read()
r = parse_file_full('src/flask/app.py', code)
print('lang:', r['lang'])
print('imports:', len(r['imports']))
for i in r['imports'][:10]: print(' ', i)
print('chunks:', len(r['chunks']))
classes = [c for c in r['chunks'] if c.get('bases')]
print(f'{len(classes)} classes with bases:')
for c in classes[:5]:
    print(f'  {c["name"]}  bases={c["bases"]}')
print('sample calls in first chunk:')
if r['chunks']:
    for x in r['chunks'][0]['calls'][:5]: print(' ', x)
