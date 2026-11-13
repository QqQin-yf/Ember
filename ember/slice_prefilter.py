"""Process a bounded amount of prepared source material without an App turn."""
import re

_COURTESY = re.compile(
    r'(?:(?:早上好|中午好|下午好|晚上好|早安|晚安|你好|谢谢你|谢谢|谢啦|不客气|辛苦了|再见|拜拜|早|嗨)'
    r'[啊呀啦喔哦哟]*|[哈哈嗯啊呀哦喔]+)+')


def courtesy_turn(row):
    messages = row['messages']
    if [m['role'] for m in messages] != ['user', 'assistant']:
        return False
    for message in messages:
        text = re.sub(r'[\W_]+', '', message['content']).casefold()
        text = re.sub(r'^(?:honey|宝宝)|(?:honey|宝宝)$', '', text)
        if not _COURTESY.fullmatch(text):
            return False
    return True
