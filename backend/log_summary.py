"""Presentation-only duplicate error summaries. Original files stay unchanged."""
import re


def compact(rows):
    result, groups = [], {}
    for level, source, line in rows:
        if level not in ('error', 'warning'):
            result.append((level, source, line))
            continue
        match = re.search(r'\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d', line)
        stamp = match.group() if match else '未提供时间'
        message = re.sub(r'\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?', '', line).strip()
        key = level, source, message
        if key not in groups:
            groups[key] = [len(result), 1, stamp]
            result.append((level, source, line))
        else:
            index, count, first = groups[key]
            groups[key][1] = count + 1
            result[index] = (level, source, f'{message} · 重复 {count+1} 次 · 首次 {first} · 最近 {stamp}')
    return result
