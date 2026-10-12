"""Load exact UTF-8 text or rebuild derived indexes; no text normalization."""
import argparse
import json
from pathlib import Path

from .postgres import PostgresStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=None, help='PostgreSQL DSN; defaults to DATABASE_URL')
    commands = parser.add_subparsers(dest='command', required=True)
    load = commands.add_parser('load')
    load.add_argument('path', type=Path)
    load.add_argument('--id', required=True)
    load.add_argument('--title', default='')
    commands.add_parser('reindex')
    commands.add_parser('republish')
    args = parser.parse_args()
    store = PostgresStore(args.db, initialize=True)
    if args.command == 'load':
        doc = store.create_document(args.id, args.path.read_bytes().decode('utf-8'), args.title or args.path.stem)
        print(json.dumps({'document_id':doc.document_id, 'version':doc.version, 'characters':len(doc.text)}))
    elif args.command == 'reindex':
        with store.connection() as db:
            heads = db.execute('SELECT id,version FROM documents').fetchall()
        for head in heads:
            store.index_revision(head['id'], head['version'], force=True)
        print(f'Indexed {len(heads)} document heads')
    else:
        with store.connection(True) as db:
            db.execute('UPDATE outbox SET published=0')
        print('Outbox retained events scheduled for republication')


if __name__ == '__main__':
    main()
