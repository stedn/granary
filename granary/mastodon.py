# coding=utf-8
"""Mastodon source class.

Mastodon is an ActivityPub implementation, but it also has a REST + OAuth 2 API
independent of AP. API docs: https://docs.joinmastodon.org/api/
"""
import itertools
import logging
import re
import urllib.parse

from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

from . import source

API_ACCOUNT = '/api/v1/accounts/%s'
API_ACCOUNT_STATUSES = '/api/v1/accounts/%s/statuses'
API_BLOCKS = '/api/v1/blocks?limit=1000'
API_CONTEXT = '/api/v1/statuses/%s/context'
API_FAVORITE = '/api/v1/statuses/%s/favourite'
API_FAVORITED_BY = '/api/v1/statuses/%s/favourited_by'
API_MEDIA = '/api/v1/media'
API_NOTIFICATIONS = '/api/v1/notifications'
API_REBLOG = '/api/v1/statuses/%s/reblog'
API_REBLOGGED_BY = '/api/v1/statuses/%s/reblogged_by'
API_SEARCH = '/api/v2/search'
API_STATUS = '/api/v1/statuses/%s'
API_STATUSES = '/api/v1/statuses'
API_TIMELINE = '/api/v1/timelines/home'
API_VERIFY_CREDENTIALS = '/api/v1/accounts/verify_credentials'

# https://docs.joinmastodon.org/api/rest/media/#parameters
MAX_ALT_LENGTH = 420

# maps Mastodon media attachment type to AS1 objectType
# https://docs.joinmastodon.org/api/entities/#type
MEDIA_TYPES = {
  'image': 'image',
  'video': 'video',
  'gifv': 'video',
  'unknown': None,
}
# Not documented, but here's the Mastodon commit that added this limit:
# https://github.com/tootsuite/mastodon/commit/5f511324b6#diff-11783d64d04391768226f7d45a610898R40
MAX_MEDIA = 4

# copied from Mastodon's source on 2019-10-21, then revised the lookbehind
# https://github.com/tootsuite/mastodon/blob/6bee7b820dcde6d487e93b8699d4aab3e49bedc4/app/models/account.rb#L52-L53
USERNAME_RE = re.compile(r'[a-z0-9_]+([a-z0-9_\.-]+[a-z0-9_]+)?', re.IGNORECASE)
MENTION_RE  = re.compile(r'(?<![\/\w])@((' + USERNAME_RE.pattern +
                         ')(?:@[a-z0-9\.\-]+[a-z0-9]+)?)', re.IGNORECASE)

# copied from twitter.py. if we need anything better, we could copy Mastodon's:
# https://github.com/tootsuite/mastodon/blob/915f3712ae7ae44c0cbe50c9694c25e3ee87a540/app/models/tag.rb#L28-L30
HASHTAG_RE = re.compile(r'(^|\s)[#＃](\w+)\b', re.UNICODE)


class Mastodon(source.Source):
  """Mastodon source class. See file docstring and Source class for details.

  Attributes:
    instance: string, base URL of Mastodon instance, eg https://mastodon.social/
    user_id: integer, optional, current user's id (not username!) on this instance
    access_token: string, optional, OAuth access token
  """
  DOMAIN = 'N/A'
  BASE_URL = 'N/A'
  NAME = 'Mastodon'

  # https://docs.joinmastodon.org/usage/basics/#text
  TRUNCATE_TEXT_LENGTH = 500
  TRUNCATE_URL_LENGTH = 23

  def __init__(self, instance, access_token, user_id=None):
    """Constructor.

    If user_id is not provided, it will be fetched via the API.

    Args:
      instance: string, base URL of Mastodon instance, eg https://mastodon.social/
      user_id: string or integer, optional, current user's id (not username!) on
        this instance
      access_token: string, optional OAuth access token
    """
    assert instance
    self.instance = self.BASE_URL = instance
    assert access_token
    self.access_token = access_token
    self.DOMAIN = util.domain_from_link(instance)

    if user_id:
      self.user_id = user_id
    else:
      creds = self._get(API_VERIFY_CREDENTIALS)
      self.user_id = creds['id']

  def user_url(self, username):
    return urllib.parse.urljoin(self.instance, '@' + username)

  def _get(self, *args, **kwargs):
    return self._api(util.requests_get, *args, **kwargs)

  def _post(self, *args, **kwargs):
    return self._api(util.requests_post, *args, **kwargs)

  def _delete(self, *args, **kwargs):
    return self._api(util.requests_delete, *args, **kwargs)

  def _api(self, fn, path, return_json=True, *args, **kwargs):
    headers = kwargs.setdefault('headers', {})
    headers['Authorization'] = 'Bearer ' + self.access_token

    url = urllib.parse.urljoin(self.instance, path)
    resp = fn(url, *args, **kwargs)
    try:
      resp.raise_for_status()
    except BaseException as e:
      util.interpret_http_exception(e)
      raise

    if not return_json:
      return resp
    if fn == util.requests_delete:
      return {}
    else:
      return json_loads(resp.text)

  @classmethod
  def embed_post(cls, obj):
    """Returns the HTML string for embedding a toot.

    https://dev.twitter.com/docs/embedded-tweets

    Args:
      obj: AS1 dict with at least url, and optionally also content.

    Returns: string, HTML
    """
    return """
  <script src="%s" async="async"></script>
  <br>
  <iframe src="%s/embed" class="mastodon-embed shadow"
          style="max-width: 100%%; border: 0" width="400"
          allowfullscreen="allowfullscreen">
  </iframe>
  """ % (urllib.parse.urljoin(obj['url'], '/embed.js'), obj['url'])

  def get_activities_response(self, user_id=None, group_id=None, app_id=None,
                              activity_id=None, fetch_replies=False,
                              fetch_likes=False, fetch_shares=False,
                              fetch_events=False, fetch_mentions=False,
                              search_query=None, start_index=0, count=0,
                              cache=None, **kwargs):
    """Fetches toots and converts them to ActivityStreams activities.

    See :meth:`Source.get_activities_response` for details.
    """
    if user_id and group_id in (source.FRIENDS, source.ALL):
      raise ValueError("Mastodon doesn't support group_id %s with user_id" % group_id)

    if not user_id:
      user_id = self.user_id
    if fetch_events:
      raise NotImplementedError()

    params = {}
    if count:
      params['limit'] = count + start_index

    if activity_id:
      statuses = [self._get(API_STATUS % activity_id)]
    elif group_id in (None, source.FRIENDS):
      statuses = self._get(API_TIMELINE, params=params)
    elif group_id == source.SEARCH:
      if not search_query:
        raise ValueError('search requires search_query parameter')
      statuses = self._get(API_SEARCH, params={
        'q': search_query,
        'resolve': True,
        'offset': start_index,
        'limit': count if count else '',
      }).get('statuses', [])
    else:  # eg group_id SELF
      statuses = self._get(API_ACCOUNT_STATUSES % user_id, params=params)

    activities = []

    # batch get memcached counts of favorites and retweets for all tweets
    cached = {}
    if cache is not None:
      keys = itertools.product(('AMRE', 'AMF', 'AMRB'), [s['id'] for s in statuses])
      cached = cache.get_multi('%s %s' % (prefix, id) for prefix, id in keys)
    # only update the cache at the end, in case we hit an error before then
    cache_updates = {}

    # fetch extras if necessary
    for status in statuses[start_index:]:
      activity = self.postprocess_activity(self.status_to_activity(status))
      activities.append(activity)

      id = status.get('id')
      if not id:
        continue

      obj = activity['object']
      count = status.get('replies_count')
      if fetch_replies and count and count != cached.get('AMRE ' + id):
        context = self._get(API_CONTEXT % id)
        obj['replies'] = {
          'items': [self.status_to_activity(reply)
                    for reply in context.get('descendants', [])]
        }
        cache_updates['AMRE ' + id] = count

      tags = obj.setdefault('tags', [])
      count = status.get('favourites_count')
      if fetch_likes and count and count != cached.get('AMF ' + id):
        likers = self._get(API_FAVORITED_BY % id)
        tags.extend(self._make_like(status, l) for l in likers)
        cache_updates['AMF ' + id] = count

      count = status.get('reblogs_count')
      if fetch_shares and count and count != cached.get('AMRB ' + id):
        sharers = self._get(API_REBLOGGED_BY % id)
        tags.extend(self._make_share(status, s) for s in sharers)
        cache_updates['AMRB ' + id] = count

    if fetch_mentions:
      # https://docs.joinmastodon.org/api/rest/notifications/
      notifs = self._get(API_NOTIFICATIONS, json={
        'exclude_types': ['follow', 'favourite', 'reblog'],
      })
      activities.extend(self.status_to_activity(n.get('status')) for n in notifs
                        if n.get('type') == 'mention')

    resp = self.make_activities_base_response(util.trim_nulls(activities))
    if cache_updates and cache is not None:
      cache.set_multi(cache_updates)
    return resp

  def get_actor(self, user_id=None):
    """Fetches and returns an account.

    Args: user_id: string, defaults to current account

    Returns: dict, ActivityStreams actor object
    """
    if user_id is None:
      user_id = self.user_id
    return self.user_to_actor(self._get(API_ACCOUNT % user_id))

  def get_comment(self, comment_id, **kwargs):
    """Fetches and returns a comment.

    Args:
      comment_id: string status id
      **kwargs: unused

    Returns: dict, ActivityStreams object

    Raises:
      :class:`ValueError`: if comment_id is invalid
    """
    return self.status_to_object(self._get(API_STATUS % comment_id))

  def status_to_activity(self, status):
    """Converts a status to an activity.

    Args:
      status: dict, a decoded JSON status

    Returns:
      an ActivityStreams activity dict, ready to be JSON-encoded
    """
    obj = self.status_to_object(status)
    activity = {
      'verb': 'post',
      'published': obj.get('published'),
      'id': obj.get('id'),
      'url': obj.get('url'),
      'actor': obj.get('author'),
      'object': obj,
      'context': {'inReplyTo': obj.get('inReplyTo')},
    }

    reblogged = status.get('reblog')
    if reblogged:
      activity.update({
        'objectType': 'activity',
        'verb': 'share',
        'object': self.status_to_object(reblogged),
      })

    app = status.get('application')
    if app:
      activity['generator'] = {
        'displayName': app.get('name'),
        'url': app.get('website'),
      }

    return self.postprocess_activity(activity)

  def status_to_object(self, status):
    """Converts a status to an object.

    Args:
      status: dict, a decoded JSON status

    Returns:
      an ActivityStreams object dict, ready to be JSON-encoded
    """
    id = status.get('id')
    if not id:
      return {}

    obj = {
      'objectType': 'note',
      'id': self.tag_uri(id),
      'url': status.get('url'),
      'published': status.get('created_at'),
      'author': self.user_to_actor(status.get('account') or {}),
      'attachments': [],
    }

    reblog = status.get('reblog')
    base_status = reblog if reblog else status

    # media! into attachments.
    for media in status.get('media_attachments', []):
      type = media.get('type')
      att = {
        'id': self.tag_uri(media.get('id')),
        'objectType': MEDIA_TYPES.get(type),
        'displayName': media.get('description'),
      }
      url = media.get('url')
      if type == 'image':
        att['image'] = {'url': url}
      elif type in ('gifv', 'video'):
        att.update({
          'stream': {'url': url},
          'image': {'url': media.get('preview_url')},
        })
      obj['attachments'].append(att)

    if obj['attachments']:
      first = obj['attachments'][0]
      if first['objectType'] == 'video':
        obj['stream'] = first.get('stream')
      else:
        obj['image'] = first.get('image')

    # tags
    obj['tags'] = [{
      'objectType': 'person',
      'id': self.tag_uri(t.get('id')),
      'url': t.get('url'),
      'displayName': t.get('username'),
    } for t in status.get('mentions', [])] + [{
      'objectType': 'hashtag',
      'url': t.get('url'),
      'displayName': t.get('name'),
    } for t in status.get('tags', [])]

    card = status.get('card')
    if card:
      obj['tags'].append({
        'objectType': 'article',
        'url': card.get('url'),
        'displayName': card.get('title'),
        'content': card.get('description'),
        'image': {'url': card.get('image')},
      })

    # content: insert images for custom emoji
    # https://docs.joinmastodon.org/api/entities/#emoji
    content = base_status.get('content') or ''
    for emoji in base_status.get('emojis', []):
      shortcode = emoji.get('shortcode')
      url = emoji.get('url')
      if shortcode and url:
        content = re.sub(
          r'(^|[^\w]):%s:([^\w]|$)' % shortcode,
          r'\1<img alt="%s" src="%s" style="height: 1em">\2' % (shortcode, url),
          content)

    # content: add 'Boosted @username:' prefix for reblogs
    if reblog and reblog.get('content'):
      reblog_account = reblog.get('account')
      content = 'Boosted <a href="%s">@%s</a>: ' % (
        (reblog_account.get('url'), reblog_account.get('username'))) + content

    obj['content'] = util.WideUnicode(content)

    # inReplyTo
    reply_to_id = status.get('in_reply_to_id')
    if reply_to_id:
      obj['inReplyTo'] = [{
        'id': self.tag_uri(reply_to_id),
        'url': urllib.parse.urljoin(self.instance, '/web/statuses/' + reply_to_id),
      }]

    # to (ie visibility)
    visibility = status.get('visibility')
    if visibility:
      obj['to'] = [{
        'objectType': 'group',
        'alias': '@' + visibility,
      }]

    return self.postprocess_object(obj)

  def user_to_actor(self, account):
    """Converts a Mastodon account to an AS1 actor.

    Args:
      account: dict, Mastodon account

    Returns: dict, AS1 actor
    """
    domain = self.DOMAIN
    username = account.get('username')

    # parse acct. it's just username for local accounts but fully qualified
    # address for remote accounts, eg user@host.com.
    acct = account.get('acct') or ''
    split = acct.split('@')
    if len(split) in (2, 3):
      acct_username, acct_domain = split[-2:]
      if acct_domain:
        domain = acct_domain
      if not username:
        username = acct[-2]
      elif acct_username and username != acct_username:
        raise ValueError('username %s and acct %s conflict!' % (username, acct))

    if not username:
      return {}

    url = account.get('url')
    # mastodon's 'Web site' fields are HTML links, so extract their URLs
    web_sites = sum((util.extract_links(f.get('value'))
                     for f in account.get('fields', [])), [])

    return util.trim_nulls({
      'objectType': 'person',
      'id': util.tag_uri(domain, username),
      'numeric_id': account.get('id'),
      'username': username,
      'displayName': account.get('display_name') or acct or username,
      'url': url,
      'urls': [{'value': u} for u in [url] + web_sites],
      'image': {'url': account.get('avatar')},
      'published': account.get('created_at'),
      'description': account.get('note'),
    })

  def _make_like(self, status, account):
    return self._make_like_or_share(status, account, 'like')

  def _make_share(self, status, account):
    return self._make_like_or_share(status, account, 'share')

  def _make_like_or_share(self, status, account, verb):
    """Generates and returns a ActivityStreams like object.

    Args:
      status: dict, Mastodon status
      account: dict, Mastodon account
      verb: string, 'like' or 'share'

    Returns: dict, AS1 like activity
    """
    assert verb in ('like', 'share')
    label = 'favorited' if verb == 'like' else 'reblogged'
    url = status.get('url')
    account_id = account.get('id')
    return {
      'id': self.tag_uri('%s_%s_by_%s' % (status.get('id'), label, account_id)),
      'url': '%s#%s-by-%s' % (url, label, account_id),
      'objectType': 'activity',
      'verb': verb,
      'object': {'url': url},
      'author': self.user_to_actor(account),
    }

  def create(self, obj, include_link=source.OMIT_LINK, ignore_formatting=False):
    """Creates a status (aka toot), reply, boost (aka reblog), or favorite.

    https://docs.joinmastodon.org/api/rest/statuses/

    Args:
      obj: ActivityStreams object
      include_link: string
      ignore_formatting: boolean

    Returns: CreationResult whose content will be a dict with 'id', 'url', and
      'type' keys (all optional) for the newly created object (or None)
    """
    return self._create(obj, preview=False, include_link=include_link,
                        ignore_formatting=ignore_formatting)

  def preview_create(self, obj, include_link=source.OMIT_LINK,
                     ignore_formatting=False):
    """Preview creating a status (aka toot), reply, boost (aka reblog), or favorite.

    https://docs.joinmastodon.org/api/rest/statuses/

    Args:
      obj: ActivityStreams object
      include_link: string
      ignore_formatting: boolean

    Returns: CreationResult whose content will be a unicode string HTML
      snippet (or None)
    """
    return self._create(obj, preview=True, include_link=include_link,
                        ignore_formatting=ignore_formatting)

  def _create(self, obj, preview=None, include_link=source.OMIT_LINK,
              ignore_formatting=False):
    """Creates or previews a status (aka toot), reply, boost (aka reblog), or favorite.

    https://docs.joinmastodon.org/api/rest/statuses/

    Based on :meth:`Twitter._create`.

    Args:
      obj: ActivityStreams object
      preview: boolean
      include_link: string
      ignore_formatting: boolean

    Returns: CreationResult. If preview is True, the content will be a unicode
      string HTML snippet. If False, it will be a dict with 'id' and 'url' keys
      for the newly created object.
    """
    assert preview in (False, True)
    type = obj.get('objectType')
    verb = obj.get('verb')

    base_obj = self.base_object(obj)
    base_id = base_obj.get('id')
    base_url = base_obj.get('url')

    is_reply = type == 'comment' or obj.get('inReplyTo')
    is_rsvp = (verb and verb.startswith('rsvp-')) or verb == 'invite'
    atts = obj.get('attachments', [])
    images = util.dedupe_urls(util.get_list(obj, 'image') +
                              [a for a in atts if a.get('objectType') == 'image'])
    videos = util.dedupe_urls([obj] + [a for a in atts if a.get('objectType') == 'video'],
                              key='stream')
    has_media = (images or videos) and (type in ('note', 'article') or is_reply)

    # prefer displayName over content for articles
    #
    # TODO: handle activities as well as objects? ie pull out ['object'] here if
    # necessary?
    type = obj.get('objectType')
    prefer_content = type == 'note' or (base_url and is_reply)
    preview_description = ''
    content = self._content_for_create(
      obj, ignore_formatting=ignore_formatting, prefer_name=not prefer_content)

    if not content:
      if type == 'activity' and not is_rsvp:
        content = verb
      elif has_media:
        content = ''
      else:
        return source.creation_result(
          abort=False,  # keep looking for things to publish,
          error_plain='No content text found.',
          error_html='No content text found.')

    if is_reply and not base_url:
      return source.creation_result(
        abort=True,
        error_plain='Could not find a Mastodon toot to reply to.',
        error_html='Could not find a Mastodon toot to <a href="http://indiewebcamp.com/reply">reply to</a>. Check that your post has the right <a href="http://indiewebcamp.com/comment">in-reply-to</a> link.')

    # truncate and ellipsize content if necessary
    # TODO: don't count domains in remote mentions.
    # https://docs.joinmastodon.org/usage/basics/#text
    content = self.truncate(content, obj.get('url'), include_link, type)

    # linkify user mentions
    def linkify_mention(match):
      split = match.group(1).split('@')
      username = split[0]
      instance = ('https://' + split[1]) if len(split) > 1 else self.instance
      url = urllib.parse.urljoin(instance, '/@' + username)
      return '<a href="%s">@%s</a>' % (url, username)

    preview_content = MENTION_RE.sub(linkify_mention, content)

    # linkify (defaults to twitter's behavior)
    preview_content = util.linkify(preview_content, pretty=True, skip_bare_cc_tlds=True)
    tags_url = urllib.parse.urljoin(self.instance, '/tags')
    preview_content = HASHTAG_RE.sub(r'\1<a href="%s/\2">#\2</a>' % tags_url,
                                     preview_content)

    # switch on activity type
    if type == 'activity' and verb == 'like':
      if not base_url:
        return source.creation_result(
          abort=True,
          error_plain='Could not find a Mastodon toot to favorite.',
          error_html='Could not find a Mastodon toot on to <a href="http://indiewebcamp.com/favorite">favorite</a>. Check that your post has the right <a href="http://indiewebcamp.com/like">u-like-of link</a>.')

      if preview:
        preview_description += '<span class="verb">favorite</span> <a href="%s">this toot</a>: %s' % (base_url, self.embed_post(base_obj))
        return source.creation_result(description=preview_description)
      else:
        resp = self._post(API_FAVORITE % base_id)
        resp['type'] = 'like'

    elif type == 'activity' and verb == 'share':
      if not base_url:
        return source.creation_result(
          abort=True,
          error_plain='Could not find a Mastodon toot to boost.',
          error_html='Could not find a Mastodon toot to <a href="http://indiewebcamp.com/repost">boost</a>. Check that your post has the right <a href="http://indiewebcamp.com/repost">repost-of</a> link.')

      if preview:
          preview_description += '<span class="verb">boost</span> <a href="%s">this toot</a>: %s' % (base_url, self.embed_post(base_obj))
          return source.creation_result(description=preview_description)
      else:
        resp = self._post(API_REBLOG % base_id)
        resp['type'] = 'repost'

    elif type in ('note', 'article') or is_reply or is_rsvp:  # a post
      data = {'status': content}

      if is_reply:
        preview_description += '<span class="verb">reply</span> to <a href="%s">this toot</a>: %s' % (base_url, self.embed_post(base_obj))
        data['in_reply_to_id'] = base_id
      else:
        preview_description += '<span class="verb">toot</span>:'

      num_media = len(videos) + len(images)
      if num_media > MAX_MEDIA:
        videos = videos[:MAX_MEDIA]
        images = images[:max(MAX_MEDIA - len(videos), 0)]
        logging.warning('Found %d media! Only using the first %d: %r',
                        num_media, MAX_MEDIA, videos + images)

      if preview:
        media_previews = [
          '<video controls src="%s"><a href="%s">%s</a></video>' %
            (util.get_url(vid, key='stream'),
             util.get_url(vid, key='stream'),
             vid.get('displayName') or 'this video')
          for vid in videos
        ] + [
          '<img src="%s" alt="%s" />' %
            (util.get_url(img), img.get('displayName') or '')
          for img in images
        ]
        if media_previews:
          preview_content += '<br /><br />' + ' &nbsp; '.join(media_previews)
        return source.creation_result(content=preview_content,
                                      description=preview_description)

      else:
        ids = self.upload_media(videos + images)
        if ids:
          data['media_ids'] = ids
        resp = self._post(API_STATUSES, json=data)

    else:
      return source.creation_result(
        abort=False,
        error_plain='Cannot publish type=%s, verb=%s to Mastodon' % (type, verb),
        error_html='Cannot publish type=%s, verb=%s to Mastodon' % (type, verb))

    if 'url' not in resp:
      resp['url'] = base_url

    return source.creation_result(resp)

  def base_object(self, obj):
    """Returns the 'base' Mastodon object that an object operates on.

    If the object is a reply, boost, or favorite of a Mastodon post - on any
    instance - this returns that post object. The id in the returned object is
    the id of that remote post *on the local instance*. (As a Mastodon style id,
    ie an integer in a string, *not* a tag URI.)

    Uses Mastodon's search API on the local instance to determine whether a URL
    is a Mastodon post, and if it is, to find or generate an id for it on the
    local instance.

    Discovered via https://mastodon.social/@jkreeftmeijer/101245063526942536

    Args:
      obj: ActivityStreams object

    Returns:
      dict, minimal ActivityStreams object. Usually has at least id; may
      also have url, author, etc.
    """
    for field in ('inReplyTo', 'object', 'target'):
      for base in util.get_list(obj, field):
        # first, check if it's on local instance
        url = util.get_url(base)
        if url.startswith(self.instance):
          return self._postprocess_base_object(base)

        # nope; try mastodon's search API
        try:
          results = self._get(API_SEARCH, params={'q': url, 'resolve': True})
        except requests.RequestException as e:
          logging.info("%s URL %s doesn't look like Mastodon:", field, url)
          continue

        for status in results.get('statuses', []):
          if url in (status.get('url'), status.get('uri')):
            # found it!
            base = self.status_to_object(status)
            base['id'] = status['id']
            return self._postprocess_base_object(base)

    return {}

  def status_url(self, id):
    """Returns the local instance URL for a status with a given id."""
    return urllib.parse.urljoin(self.instance, '/web/statuses/%s' % id)

  def upload_media(self, media):
    """Uploads one or more images or videos from web URLs.

    https://docs.joinmastodon.org/api/rest/media/

    Args:
      media: sequence of AS image or stream objects, eg:
        [{'url': 'http://picture', 'displayName': 'a thing'}, ...]

    Returns: list of string media ids for uploaded files
    """
    uploaded = set()  # URLs uploaded so far; for de-duping
    ids = []

    for obj in media:
      url = util.get_url(obj, key='stream') or util.get_url(obj)
      if not url or url in uploaded:
        continue

      data = {}
      alt = obj.get('displayName')
      if alt:
        data['description'] = util.ellipsize(alt, chars=MAX_ALT_LENGTH)

      # TODO: mime type check?
      with util.requests_get(url, stream=True) as fetch:
        fetch.raise_for_status()
        upload = self._post(API_MEDIA, files={'file': fetch.raw})

      logging.info('Got: %s', upload)
      media_id = upload['id']
      ids.append(media_id)
      uploaded.add(url)

    return ids

  def delete(self, id):
    """Deletes a toot. The authenticated user must have authored it.

    Args:
      id: int or string, toot id (on local instance) to delete

    Returns: CreationResult, content is dict with url and id fields
    """
    self._delete(API_STATUS % id)
    return source.creation_result({'url': self.status_url(id)})

  def preview_delete(self, id):
    """Previews deleting a toot.

    Args:
      id: int or string, toot id (on local instance) to delete

    Returns: CreationResult
    """
    # can't embed right now because embeds require standalone URL, eg
    # http://foo.com/@user/123, and we don't have the username here.
    return source.creation_result(
      description='<span class="verb">delete</span> <a href="%s">this toot</a>.' %
      self.status_url(id))

  def get_blocklist_ids(self):
    """Returns the current user's block list as a list of integer account ids.

    May make multiple API calls to fully fetch large block lists.
    https://docs.joinmastodon.org/api/rest/blocks/#pagination

    Returns:
      sequence of integer Mastodon account ids on the current instance
    """
    ids = []
    url = API_BLOCKS
    while True:
      resp = self._get(url, return_json=False)
      ids.extend(util.trim_nulls([rel.get('id') for rel in json_loads(resp.text)]))
      url = resp.links.get('next', {}).get('url')
      if not url:
        break

    return ids
