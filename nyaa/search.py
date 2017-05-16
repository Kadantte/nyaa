import flask
import re
import math
import json
import shlex

from nyaa import app, db
from nyaa import models

import sqlalchemy_fulltext.modes as FullTextMode
from sqlalchemy_fulltext import FullTextSearch
from elasticsearch import Elasticsearch
from elasticsearch_dsl import Search, Q


def search_elastic(term='', user=None, sort='id', order='desc',
                   category='0_0', quality_filter='0', page=1,
                   rss=False, admin=False, logged_in_user=None,
                   per_page=75, max_search_results=1000):
    # This function can easily be memcached now

    es_client = Elasticsearch()

    es_sort_keys = {
        'id': 'id',
        'size': 'filesize',
        # 'name': 'display_name',  # This is slow and buggy
        'seeders': 'seed_count',
        'leechers': 'leech_count',
        'downloads': 'download_count'
    }

    sort_ = sort.lower()
    if sort_ not in es_sort_keys:
        flask.abort(400)

    es_sort = es_sort_keys[sort]

    order_keys = {
        'desc': 'desc',
        'asc': 'asc'
    }

    order_ = order.lower()
    if order_ not in order_keys:
        flask.abort(400)

    # Only allow ID, desc if RSS
    if rss:
        sort = es_sort_keys['id']
        order = 'desc'

    # funky, es sort is default asc, prefixed by '-' if desc
    if 'desc' == order:
        es_sort = '-' + es_sort

    # Quality filter
    quality_keys = [
        '0',  # Show all
        '1',  # No remakes
        '2',  # Only trusted
        '3'   # Only completed
    ]

    if quality_filter.lower() not in quality_keys:
        flask.abort(400)

    quality_filter = int(quality_filter)

    # Category filter
    main_category = None
    sub_category = None
    main_cat_id = 0
    sub_cat_id = 0
    if category:
        cat_match = re.match(r'^(\d+)_(\d+)$', category)
        if not cat_match:
            flask.abort(400)

        main_cat_id = int(cat_match.group(1))
        sub_cat_id = int(cat_match.group(2))

        if main_cat_id > 0:
            if sub_cat_id > 0:
                sub_category = models.SubCategory.by_category_ids(main_cat_id, sub_cat_id)
                if not sub_category:
                    flask.abort(400)
            else:
                main_category = models.MainCategory.by_id(main_cat_id)
                if not main_category:
                    flask.abort(400)

    # This might be useless since we validate users
    # before coming into this method, but just to be safe...
    if user:
        user = models.User.by_id(user)
        if not user:
            flask.abort(404)
        user = user.id

    same_user = False
    if logged_in_user:
        same_user = user == logged_in_user.id

    s = Search(using=es_client, index=app.config.get('ES_INDEX_NAME'))  # todo, sukebei prefix

    # Apply search term
    if term:
        s = s.query('simple_query_string',
                    analyzer='my_search_analyzer',
                    default_operator="AND",
                    query=term)

    # User view (/user/username)
    if user:
        s = s.filter('term', uploader_id=user)

        if not admin:
            # Hide all DELETED torrents if regular user
            s = s.filter('term', deleted=False)
            # If logged in user is not the same as the user being viewed,
            # show only torrents that aren't hidden or anonymous.
            #
            # If logged in user is the same as the user being viewed,
            # show all torrents including hidden and anonymous ones.
            #
            # On RSS pages in user view, show only torrents that
            # aren't hidden or anonymous no matter what
            if not same_user or rss:
                s = s.filter('term', hidden=False)
                s = s.filter('term', anonymous=False)
    # General view (homepage, general search view)
    else:
        if not admin:
            # Hide all DELETED torrents if regular user
            s = s.filter('term', deleted=False)
            # If logged in, show all torrents that aren't hidden unless they belong to you
            # On RSS pages, show all public torrents and nothing more.
            if logged_in_user and not rss:
                hiddenFilter = Q('term', hidden=False)
                userFilter = Q('term', uploader_id=logged_in_user.id)
                combinedFilter = hiddenFilter | userFilter
                s = s.filter('bool', filter=[combinedFilter])
            else:
                s = s.filter('term', hidden=False)

    if main_category:
        s = s.filter('term', main_category_id=main_cat_id)
    elif sub_category:
        s = s.filter('term', main_category_id=main_cat_id)
        s = s.filter('term', sub_category_id=sub_cat_id)

    if quality_filter == 0:
        pass
    elif quality_filter == 1:
        s = s.filter('term', remake=False)
    elif quality_filter == 2:
        s = s.filter('term', trusted=True)
    elif quality_filter == 3:
        s = s.filter('term', complete=True)

    # Apply sort
    s = s.sort(es_sort)

    # Only show first RESULTS_PER_PAGE items for RSS
    if rss:
        s = s[0:per_page]
    else:
        max_page = min(page, int(math.ceil(max_search_results / float(per_page))))
        from_idx = (max_page-1)*per_page
        to_idx = min(max_search_results, max_page*per_page)
        s = s[from_idx:to_idx]

    highlight = app.config.get('ENABLE_ELASTIC_SEARCH_HIGHLIGHT')
    if highlight:
        s = s.highlight_options(tags_schema='styled')
        s = s.highlight("display_name")

    # Return query, uncomment print line to debug query
    # from pprint import pprint
    # print(json.dumps(s.to_dict()))
    return s.execute()


def search_db(term='', user=None, sort='id', order='desc', category='0_0',
              quality_filter='0', page=1, rss=False, admin=False,
              logged_in_user=None, per_page=75):
    sort_keys = {
        'id': models.Torrent.id,
        'size': models.Torrent.filesize,
        # 'name': models.Torrent.display_name, # Disable this because we disabled this in search_elastic, for the sake of consistency
        'seeders': models.Statistic.seed_count,
        'leechers': models.Statistic.leech_count,
        'downloads': models.Statistic.download_count
    }

    sort_ = sort.lower()
    if sort_ not in sort_keys:
        flask.abort(400)
    sort = sort_keys[sort]

    order_keys = {
        'desc': 'desc',
        'asc': 'asc'
    }

    order_ = order.lower()
    if order_ not in order_keys:
        flask.abort(400)

    filter_keys = {
        '0': None,
        '1': (models.TorrentFlags.REMAKE, False),
        '2': (models.TorrentFlags.TRUSTED, True),
        '3': (models.TorrentFlags.COMPLETE, True)
    }

    sentinel = object()
    filter_tuple = filter_keys.get(quality_filter.lower(), sentinel)
    if filter_tuple is sentinel:
        flask.abort(400)

    if user:
        user = models.User.by_id(user)
        if not user:
            flask.abort(404)
        user = user.id

    main_category = None
    sub_category = None
    main_cat_id = 0
    sub_cat_id = 0
    if category:
        cat_match = re.match(r'^(\d+)_(\d+)$', category)
        if not cat_match:
            flask.abort(400)

        main_cat_id = int(cat_match.group(1))
        sub_cat_id = int(cat_match.group(2))

        if main_cat_id > 0:
            if sub_cat_id > 0:
                sub_category = models.SubCategory.by_category_ids(main_cat_id, sub_cat_id)
            else:
                main_category = models.MainCategory.by_id(main_cat_id)

            if not category:
                flask.abort(400)

    # Force sort by id desc if rss
    if rss:
        sort = sort_keys['id']
        order = 'desc'

    same_user = False
    if logged_in_user:
        same_user = logged_in_user.id == user

    if term:
        query = db.session.query(models.TorrentNameSearch)
    else:
        query = models.Torrent.query

    # User view (/user/username)
    if user:
        query = query.filter(models.Torrent.uploader_id == user)

        if not admin:
            # Hide all DELETED torrents if regular user
            query = query.filter(models.Torrent.flags.op('&')(int(models.TorrentFlags.DELETED)).is_(False))
            # If logged in user is not the same as the user being viewed, show only torrents that aren't hidden or anonymous
            # If logged in user is the same as the user being viewed, show all torrents including hidden and anonymous ones
            # On RSS pages in user view, show only torrents that aren't hidden or anonymous no matter what
            if not same_user or rss:
                query = query.filter(models.Torrent.flags.op('&')(int(models.TorrentFlags.HIDDEN |
                                                                      models.TorrentFlags.ANONYMOUS)).is_(False))
    # General view (homepage, general search view)
    else:
        if not admin:
            # Hide all DELETED torrents if regular user
            query = query.filter(models.Torrent.flags.op('&')(int(models.TorrentFlags.DELETED)).is_(False))
            # If logged in, show all torrents that aren't hidden unless they belong to you
            # On RSS pages, show all public torrents and nothing more.
            if logged_in_user and not rss:
                query = query.filter((models.Torrent.flags.op('&')(int(models.TorrentFlags.HIDDEN)).is_(False)) |
                                     (models.Torrent.uploader_id == logged_in_user.id))
            # Otherwise, show all torrents that aren't hidden
            else:
                query = query.filter(models.Torrent.flags.op('&')(int(models.TorrentFlags.HIDDEN)).is_(False))

    if main_category:
        query = query.filter(models.Torrent.main_category_id == main_cat_id)
    elif sub_category:
        query = query.filter((models.Torrent.main_category_id == main_cat_id) &
                             (models.Torrent.sub_category_id == sub_cat_id))

    if filter_tuple:
        query = query.filter(models.Torrent.flags.op('&')(int(filter_tuple[0])).is_(filter_tuple[1]))

    if term:
        for item in shlex.split(term, posix=False):
            if len(item) >= 2:
                query = query.filter(FullTextSearch(
                    item, models.TorrentNameSearch, FullTextMode.NATURAL))

    # Sort and order
    if sort.class_ != models.Torrent:
        query = query.join(sort.class_)

    query = query.order_by(getattr(sort, order)())

    if rss:
        query = query.limit(per_page)
    else:
        query = query.paginate_faste(page, per_page=per_page, step=5)

    return query