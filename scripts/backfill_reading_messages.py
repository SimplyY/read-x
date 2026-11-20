#!/usr/bin/env python3
"""Recover Read-X group text messages missed by the live Bridge."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from archive_reading_context import save_journal
from reading_intake import register_event, _open_database

APP = 'cli_aab030a473f8dcd2'
CHAT = 'oc_f79c12bcb643eb26c486282f933333a0'
URL = re.compile(r'''https?://[^\s<>"']+''')
TRAILING = ',.!?，。！？；'


def urls_for(content):
    urls = []
    for match in URL.finditer(content):
        value = match.group().rstrip(TRAILING)
        for left, right in [('(', ')'), ('（', '）'), ('【', '】')]:
            while value.endswith(right) and value.count(right) > value.count(left):
                value = value[:-1].rstrip(TRAILING)
        urls.append(value)
    return urls


def cli(args):
    result = subprocess.run(['lark-cli', *args, '--format', 'json'], capture_output=True, text=True, timeout=90)
    data = json.loads(result.stdout)
    if result.returncode or data.get('ok') is not True:
        raise RuntimeError('message history unavailable')
    return data


def scan(messages, owner, register, *, nested=False):
    accepted = 0
    for message in messages:
        replies = message.get('thread_replies') or []
        if not isinstance(replies, list):
            raise ValueError('unsupported thread replies')
        accepted += scan(replies, owner, register, nested=True)
        sender = message.get('sender') or {}
        if sender.get('sender_type') != 'user' or sender.get('id_type') != 'open_id' or sender.get('id') != owner:
            continue
        if message.get('deleted') or message.get('msg_type') not in ('text', 'post'):
            continue
        content = message.get('content')
        if not isinstance(content, str):
            raise ValueError('unsupported message content')
        urls = urls_for(content)
        if not urls:
            continue
        event = {'appId':APP, 'chatId':CHAT, 'chatType':'group', 'senderId':owner,
                 'senderType':'user', 'messageId':message.get('message_id'), 'urls':urls,
                 'scoreOnly':bool(re.fullmatch(r'仅评分\s+https?://\S+',content.strip()))}
        if message.get('create_time'):
            event['sourceCreatedAt'] = str(message['create_time'])
        if message.get('thread_id'):
            event['threadId'] = message['thread_id']
        elif nested:
            event['routingUncertain'] = True
        result = register(event)
        accepted += result['articles']
    return accepted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'state/reading-workbench')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    identity = subprocess.run(['lark-cli','auth','status','--json','--verify'],capture_output=True,text=True,timeout=30)
    info = json.loads(identity.stdout)
    user = info.get('identities',{}).get('user',{})
    if identity.returncode or info.get('appId') != APP or not user.get('verified') or not user.get('openId'):
        raise RuntimeError('user identity not verified')
    now = datetime.now(timezone.utc)
    checkpoint = args.state_dir / 'message-watermark.json'
    if checkpoint.exists():
        previous = json.loads(checkpoint.read_text())['last_complete']
        start = datetime.fromisoformat(previous) - timedelta(minutes=5)
    else:
        start = datetime(2026,11,17,16,tzinfo=timezone.utc)
    response = cli(['im','+chat-messages-list','--chat-id',CHAT,'--as','bot','--no-reactions',
                '--start',start.isoformat(),'--end',now.isoformat(),'--order','asc','--page-all','--page-limit','1000'])
    data = response.get('data') or {}
    pagination = response.get('meta', {}).get('pagination', {})
    if pagination.get('complete') is not True or data.get('has_more') or not isinstance(data.get('messages'),list):
        raise RuntimeError('message history incomplete')
    if not args.execute:
        print(json.dumps({'status':'dry_run','messages':len(data['messages'])})); return 0
    owner = user['openId']
    _open_database(args.state_dir).close()
    count = scan(data['messages'], owner, lambda event: register_event(args.state_dir,event,
        allowed_app_id=APP,allowed_chat_id=CHAT,allowed_sender_id=owner))
    save_journal(checkpoint, {'last_complete':now.isoformat()})
    print(json.dumps({'status':'ok','messages':len(data['messages']),'new_articles':count}))
    return 0

if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        print(json.dumps({'status':'needs_review','reason':'message-history-unavailable-or-invalid'}),file=sys.stderr)
        raise SystemExit(1)
