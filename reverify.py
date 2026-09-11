# -*- coding: utf-8 -*-
"""重验指定字集（SC），结果写独立文件，不动 verifyOut。用法:
python reverify.py <chars文件> <输出jsonl> [起始] [数量]"""
import sys, json
from multiprocessing import Pool
import strokelab.verify as V

def main():
    charsFile, outFile = sys.argv[1], sys.argv[2]
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    cnt = int(sys.argv[4]) if len(sys.argv) > 4 else 10**9
    jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 7
    chars = open(charsFile, encoding='utf-8').read().strip()
    chars = chars[start:start+cnt]
    with open(outFile, 'a', encoding='utf-8') as f:
        pool = None
        if jobs <= 1:
            V._initWorker('.', 'Fonts/HarmonyOS_Sans_SC.ttf')
            iterator = map(V._verifyOne, chars)
        else:
            pool = Pool(jobs, initializer=V._initWorker,
                        initargs=('.', 'Fonts/HarmonyOS_Sans_SC.ttf'))
            iterator = pool.imap_unordered(V._verifyOne, chars, chunksize=4)
        try:
            for i, line in enumerate(iterator):
                f.write(line + '\n')
                if (i+1) % 200 == 0:
                    print(i+1, flush=True)
        finally:
            if pool is not None:
                pool.close(); pool.join()
    print('done', len(chars), flush=True)

if __name__ == '__main__':
    main()
