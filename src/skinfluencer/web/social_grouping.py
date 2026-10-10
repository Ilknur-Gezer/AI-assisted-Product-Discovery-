"""Conservative display grouping for verified cross-platform reposts.

Never collapses records solely on matching captions or dates.
"""
from __future__ import annotations


def group_social_posts(posts, exact_pairs=()):
    """Group the same product/creator's exact matched Instagram/TikTok posts.

    Input rows are mappings returned by get_social_posts(). The returned
    dictionaries preserve source links and prefer meaningful approved context.
    """
    rows = [dict(post) for post in posts]
    by_id = {int(row['social_post_id']): i for i, row in enumerate(rows)}
    parent = list(range(len(rows)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for match in exact_pairs:
        # Backward-compatible exact (left, right), or (left, right, type, similarity).
        left, right = match[:2]
        match_type = match[2] if len(match) > 2 else "exact"
        similarity = match[3] if len(match) > 3 else 1.0
        i, j = by_id.get(int(left)), by_id.get(int(right))
        if i is None or j is None:
            continue
        a, b = rows[i], rows[j]
        if (a['product_id'], a['influencer_slug']) != (b['product_id'], b['influencer_slug']):
            continue
        if a['platform'] == b['platform'] or {a['platform'], b['platform']} != {'instagram', 'tiktok'}:
            continue
        if match_type == 'near':
            # No inferred joins from dates/captions: a recorded match is mandatory.
            # A near match additionally needs the SAME calendar date and high similarity.
            date_a = str(a.get('published_at') or '')[:10]
            date_b = str(b.get('published_at') or '')[:10]
            if not date_a or date_a != date_b or similarity is None or float(similarity) < 0.90:
                continue
        elif match_type != 'exact':
            continue
        parent[root(j)] = root(i)

    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(root(i), []).append(row)

    output = []
    for members in groups.values():
        def quality(row):
            approved = row.get('caption_context_status') == 'approved'
            meaningful = row.get('caption_relationship') not in (None, '', 'mention_only')
            summary = bool(row.get('localized_caption_opinion_summary') or row.get('localized_caption_summary'))
            return (approved and meaningful and summary, row.get('platform') == 'tiktok')
        winner = dict(max(members, key=quality))
        links = {}
        for member in members:
            platform = member.get('platform')
            url = str(member.get('original_url') or '').strip()
            if platform in ('tiktok', 'instagram') and url:
                links[platform] = url
            if member.get('instagram_url'):
                links['instagram'] = member['instagram_url']
        winner['tiktok_url'] = links.get('tiktok', '')
        winner['instagram_url'] = links.get('instagram', '')
        output.append(winner)
    return output
