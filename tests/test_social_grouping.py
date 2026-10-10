from skinfluencer.web.social_grouping import group_social_posts


def post(i, platform, product=1, creator='cisem', summary='', status=''):
    return {'social_post_id': i, 'platform': platform, 'product_id': product,
            'influencer_slug': creator, 'original_url': f'https://{platform}/{i}',
            'caption_context_status': status, 'caption_relationship': 'recommendation',
            'localized_caption_summary': summary, 'localized_caption_opinion_summary': '',
            'instagram_url': ''}


def test_exact_repost_combines_links_and_prefers_approved_opinion():
    rows = [post(1, 'tiktok'), post(2, 'instagram', summary='Great serum', status='approved')]
    result = group_social_posts(rows, [(1, 2)])
    assert len(result) == 1
    assert result[0]['localized_caption_summary'] == 'Great serum'
    assert result[0]['tiktok_url'] == 'https://tiktok/1'
    assert result[0]['instagram_url'] == 'https://instagram/2'


def test_no_merge_without_explicit_match():
    assert len(group_social_posts([post(1, 'tiktok'), post(2, 'instagram')])) == 2


def test_never_merge_different_products_or_creators():
    rows = [post(1, 'tiktok'), post(2, 'instagram', product=9),
            post(3, 'instagram', creator='other')]
    assert len(group_social_posts(rows, [(1, 2), (1, 3)])) == 3
