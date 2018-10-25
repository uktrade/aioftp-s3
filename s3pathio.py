from collections import (
    namedtuple,
)
from datetime import datetime
import hashlib
import hmac
import io
import urllib
import xml.etree.ElementTree as ET

from aioftp.pathio import (
    universal_exception,
)


Stat = namedtuple(
    'Stat',
    ['st_size', 'st_mtime', 'st_ctime', 'st_nlink', 'st_mode'],
)

Node = namedtuple(
    'Node',
    ['name', 'type', 'stat'],
)

AwsCredentials = namedtuple('AwsCredentials', [
    'access_key_id', 'secret_access_key', 'pre_auth_headers',
])

AwsS3Bucket = namedtuple('AwsS3Bucket', [
    'region', 'host', 'name',
])


def s3_path_io_factory(session, credentials, bucket):

    # The aioftp way of configuring the path with a "nursery" doesn't
    # seem that configurable in terms of doing things per instance,
    # so make our own that effectively bypasses it
    def factory(*_, **__):
        return S3PathIO(session, credentials, bucket)

    return factory


def s3_path_io_secret_access_key_credentials(access_key_id, secret_access_key):

    async def get():
        return AwsCredentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            pre_auth_headers={},
        )

    return get


def s3_path_io_bucket(region, host, name):
    return AwsS3Bucket(
        region=region,
        host=host,
        name=name,
    )


class S3PathIO():

    def __init__(self, session, credentials, bucket):
        self.session = session
        self.credentials = credentials
        self.bucket = bucket

        # The aioftp's mechanism for state doesn't seem that
        # configurable per instance, so we don't use it. However,
        # it does expect a member called state
        self.state = None

    @universal_exception
    async def exists(self, _):
        return True

    @universal_exception
    async def is_dir(self, node):
        return node.type == 'dir'

    @universal_exception
    async def is_file(self, node):
        return node.type == 'file'

    @universal_exception
    async def mkdir(self, path, *, parents=False, exist_ok=False):
        raise NotImplementedError

    @universal_exception
    async def rmdir(self, path):
        raise NotImplementedError

    @universal_exception
    async def unlink(self, path):
        raise NotImplementedError

    def list(self, path):
        return _list(self.session, self.credentials, self.bucket, path)

    @universal_exception
    async def stat(self, node):
        return node.stat

    @universal_exception
    async def _open(self, path, mode):
        raise NotImplementedError

    @universal_exception
    async def seek(self, file, offset, whence=io.SEEK_SET):
        raise NotImplementedError

    @universal_exception
    async def write(self, file, data):
        raise NotImplementedError

    @universal_exception
    async def read(self, file, block_size):
        raise NotImplementedError

    @universal_exception
    async def close(self, file):
        raise NotImplementedError

    @universal_exception
    async def rename(self, source, destination):
        raise NotImplementedError


def _key(_):
    return ''


async def _list(session, creds, bucket, path):
    for node in await _list_immediate_child_nodes(session, creds, bucket, _key(path)):
        yield node


async def _list_immediate_child_nodes(session, creds, bucket, key_prefix):
    return await _list_nodes(session, creds, bucket, key_prefix, '/')


async def _list_nodes(session, creds, bucket, key_prefix, delimeter):
    epoch = datetime.utcfromtimestamp(0)
    common_query = {
        'max-keys': '1000',
        'list-type': '2',
    }

    async def _list_first_page():
        query = {
            **common_query,
            'delimiter': delimeter,
            'prefix': key_prefix,
        }
        _, body = await _make_s3_request(session, creds, bucket, 'GET', '/', query, {}, b'')
        return _parse_list_response(body)

    async def _list_later_page(token):
        query = {
            **common_query,
            'continuation-token': token,
        }
        _, body = await _make_s3_request(session, creds, bucket, 'GET', '/', query, {}, b'')
        return _parse_list_response(body)

    def _first_child_text(element, tag):
        for child in element:
            if child.tag == tag:
                return child.text
        return None

    def _parse_list_response(body):
        namespace = '{http://s3.amazonaws.com/doc/2006-03-01/}'
        root = ET.fromstring(body)
        next_token = ''
        nodes = []
        for element in root:
            if element.tag == f'{namespace}Contents':
                key = _first_child_text(element, f'{namespace}Key')
                last_modified_str = _first_child_text(element, f'{namespace}LastModified')
                last_modified_datetime = datetime.strptime(
                    last_modified_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                last_modified_since_epoch_seconds = int(
                    (last_modified_datetime - epoch).total_seconds())
                nodes.append(Node(
                    name=key,
                    type='file',
                    stat=Stat(
                        st_size=1,
                        st_mtime=last_modified_since_epoch_seconds,
                        st_ctime=last_modified_since_epoch_seconds,
                        st_nlink=1,
                        st_mode=0o100666,  # stat.S_IFREG | 0o666
                    )))

            if element.tag == f'{namespace}CommonPrefixes':
                # Prefixes end in '/', which we strip off
                key_prefix = _first_child_text(element, f'{namespace}Prefix')[:-1]
                nodes.append(Node(
                    name=key_prefix,
                    type='dir',
                    stat=Stat(
                        # Not completely sure what size should be for a directory
                        st_size=0,
                        # Can't quite work out an efficient way of working out
                        # any sort of meaningful modification/creation time for a
                        # directory
                        st_mtime=0,
                        st_ctime=0,
                        st_nlink=1,
                        st_mode=0o40777,  # stat.S_IFDIR | 0o777
                    ),
                ))

            if element.tag == f'{namespace}NextContinuationToken':
                next_token = element.text

        return (next_token, nodes)

    token, nodes = await _list_first_page()
    while token:
        token, nodes_page = await _list_later_page(token)
        nodes.extend(nodes_page)

    return nodes


async def _make_s3_request(session, credentials, bucket,
                           method, path, query, api_pre_auth_headers, payload):

    service = 's3'
    creds = await credentials()
    pre_auth_headers = {
        **api_pre_auth_headers,
        **creds.pre_auth_headers,
    }
    full_path = f'/{bucket.name}{path}'
    headers = _aws_sig_v4_headers(
        creds.access_key_id, creds.secret_access_key, pre_auth_headers,
        service, bucket.region, bucket.host, method, full_path, query, payload,
    )

    querystring = urllib.parse.urlencode(query, safe='~', quote_via=urllib.parse.quote)
    encoded_path = urllib.parse.quote(full_path, safe='/~')
    url = f'https://{bucket.host}{encoded_path}' + (('?' + querystring) if querystring else '')

    async with session.request(method, url, headers=headers, data=payload) as result:
        return result, await result.read()


def _aws_sig_v4_headers(access_key_id, secret_access_key, pre_auth_headers,
                        service, region, host, method, path, query, payload):
    algorithm = 'AWS4-HMAC-SHA256'

    now = datetime.utcnow()
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    payload_hash = hashlib.sha256(payload).hexdigest()
    credential_scope = f'{datestamp}/{region}/{service}/aws4_request'

    pre_auth_headers_lower = {
        header_key.lower(): ' '.join(header_value.split())
        for header_key, header_value in pre_auth_headers.items()
    }
    required_headers = {
        'host': host,
        'x-amz-content-sha256': payload_hash,
        'x-amz-date': amzdate,
    }
    headers = {**pre_auth_headers_lower, **required_headers}
    header_keys = sorted(headers.keys())
    signed_headers = ';'.join(header_keys)

    def signature():
        def canonical_request():
            canonical_uri = urllib.parse.quote(path, safe='/~')
            quoted_query = sorted(
                (urllib.parse.quote(key, safe='~'), urllib.parse.quote(value, safe='~'))
                for key, value in query.items()
            )
            canonical_querystring = '&'.join(f'{key}={value}' for key, value in quoted_query)
            canonical_headers = ''.join(f'{key}:{headers[key]}\n' for key in header_keys)

            return f'{method}\n{canonical_uri}\n{canonical_querystring}\n' + \
                   f'{canonical_headers}\n{signed_headers}\n{payload_hash}'

        def sign(key, msg):
            return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

        string_to_sign = f'{algorithm}\n{amzdate}\n{credential_scope}\n' + \
                         hashlib.sha256(canonical_request().encode('utf-8')).hexdigest()

        date_key = sign(('AWS4' + secret_access_key).encode('utf-8'), datestamp)
        region_key = sign(date_key, region)
        service_key = sign(region_key, service)
        request_key = sign(service_key, 'aws4_request')
        return sign(request_key, string_to_sign).hex()

    return {
        **pre_auth_headers,
        'x-amz-date': amzdate,
        'x-amz-content-sha256': payload_hash,
        'Authorization': f'{algorithm} Credential={access_key_id}/{credential_scope}, '
                         f'SignedHeaders={signed_headers}, Signature=' + signature(),
    }
