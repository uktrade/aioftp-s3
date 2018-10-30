import asyncio
from collections import (
    deque,
    namedtuple,
)
from contextlib import (
    AsyncExitStack,
    asynccontextmanager,
)
from datetime import datetime
import hashlib
import hmac
from pathlib import PurePosixPath
import re
import urllib
import weakref
import xml.etree.ElementTree as ET

from aioftp.pathio import (
    universal_exception,
)


# This must be between 5 and 2000MB
MULTIPART_UPLOAD_MIN_BYTES = 1024 * 1024 * 25

# How long we sleep per incoming write if uploads haven't kept up
MULTIPART_UPLOAD_IF_SLOW_SLEEP_SECONDS = 1

# How many in-progress uploads per file we allow per uploading file
MULTIPART_UPLOAD_MAX_CONCURRENT_UPLOADS_PER_FILE = 3

REG_MODE = 0o10666  # stat.S_IFREG | 0o666
DIR_MODE = 0o40777  # stat.S_IFDIR | 0o777

# The S3 console uses '/' as both the folder separator for
# navigation, and as the suffix for objects created when
# you create a folder, so we do exacty the same here
S3_DIR_SUFFIX = '/'

Stat = namedtuple(
    'Stat',
    ['st_size', 'st_mtime', 'st_ctime', 'st_nlink', 'st_mode'],
)

AwsCredentials = namedtuple('AwsCredentials', [
    'access_key_id', 'secret_access_key', 'pre_auth_headers',
])

AwsS3Bucket = namedtuple('AwsS3Bucket', [
    'region', 'host', 'verify_certs', 'name',
])

Context = namedtuple('Context', [
    'session', 'credentials', 'bucket',
])

ListKey = namedtuple('ListKey', [
    'key', 'size', 'last_modified',
])

ListPrefix = namedtuple('ListPrefix', [
    'prefix',
])


class S3Path(PurePosixPath):
    ''' The purpose of this class is to be able to integrate with
    code in aioftp that expects a Path instance, but be able to
    attach more data that is returned from S3 List Objects which
    we access from S3PathIO
    '''

    def __new__(cls, *args, stat):
        self = super().__new__(cls, *args)
        self.stat = stat
        return self


def s3_path_io_factory(session, credentials, bucket):

    lock = _PathLock()

    # The aioftp way of configuring the path with a "nursery" doesn't
    # seem that configurable in terms of doing things per instance,
    # so make our own that effectively bypasses it
    def factory(*_, **__):
        return S3PathIO(session, credentials, bucket, lock)

    return factory


def s3_path_io_secret_access_key_credentials(access_key_id, secret_access_key):

    async def get():
        return AwsCredentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            pre_auth_headers={},
        )

    return get


def s3_path_io_bucket(region, host, verify_certs, name):
    return AwsS3Bucket(
        region=region,
        host=host,
        verify_certs=verify_certs,
        name=name,
    )


class S3PathIO():

    def __init__(self, session, credentials, bucket, lock):
        self.session = session
        self.credentials = credentials
        self.bucket = bucket
        self.lock = lock

        # The aioftp's mechanism for state doesn't seem that
        # configurable per instance, so we don't use it. However,
        # it does expect a member called state
        self.state = None

    @universal_exception
    async def exists(self, path):
        return await _exists(self._context(), path)

    @universal_exception
    async def is_dir(self, path):
        return await _is_dir(self._context(), path)

    @universal_exception
    async def is_file(self, path):
        return await _is_file(self._context(), path)

    @universal_exception
    async def mkdir(self, path, *_, **__):
        context = self._context()
        async with self.lock(write_to=[path], read_from=[]):
            if await _exists(context, path):
                raise Exception('{} already exists'.format(path))

            await _mkdir(context, path)

    @universal_exception
    async def rmdir(self, path):
        context = self._context()
        async with self.lock(write_to=[path], read_from=[]):
            if await _is_file(context, path):
                raise Exception('{} is not a directory'.format(path))

            if not await _is_dir(context, path):
                raise Exception('{} does not exist'.format(path))

            return await _rmdir(context, path)

    @universal_exception
    async def unlink(self, path):
        context = self._context()
        async with self.lock(write_to=[path], read_from=[]):
            if await _is_dir(context, path):
                raise Exception('{} is a directory'.format(path))

            if not await _is_file(context, path):
                raise Exception('{} does not exist'.format(path))

            return await _unlink(context, path)

    def list(self, path):
        # We allow lists to see non-consistent changes: renames or deletes
        # of folders with lots of files could be taking a while, and nothing
        # too horrible will happen if there is a list half way through
        return _list(self._context(), path)

    @universal_exception
    async def stat(self, path):
        return path.stat

    def open(self, path, mode):
        return OPENERS[mode](self._context(), self.lock, path)

    @universal_exception
    async def rename(self, source, destination):
        raise NotImplementedError

    def _context(self):
        return Context(
            session=self.session,
            credentials=self.credentials,
            bucket=self.bucket,
        )


async def _exists(context, path):
    return \
        await _is_file(context, path) or \
        await _is_dir(context, path)


async def _is_dir(context, path):
    result = \
        True if isinstance(path, S3Path) and path.stat.st_mode & DIR_MODE else \
        True if path == PurePosixPath('.') else \
        await _dir_exists(context, path)
    return result


async def _is_file(context, path):
    result = \
        True if isinstance(path, S3Path) and path.stat.st_mode & REG_MODE else \
        False if path == PurePosixPath('.') else \
        await _file_exists(context, path)
    return result


async def _file_exists(context, path):
    key = path.as_posix()
    response, _ = await _s3_request_full(context, 'HEAD', '/' + key, {}, {},
                                         b'', _hash(b''))
    return response.status == 200


async def _dir_exists(context, path):
    key = path.as_posix()
    response, _ = await _s3_request_full(context, 'HEAD', '/' + key + S3_DIR_SUFFIX, {}, {},
                                         b'', _hash(b''))
    return response.status == 200


async def _mkdir(context, path):
    key = path.as_posix() + '/'
    response, _ = await _s3_request_full(context, 'PUT', '/' + key, {}, {}, b'', _hash(b''))
    response.raise_for_status()


async def _rmdir(context, path):
    key_prefix = path.as_posix() + '/'
    keys = await _list_descendant_keys(context, key_prefix)

    def delete_sort_key(key):
        # Delete innermost files and folders first
        return (key.key.count('/'), len(key.key), key.key)

    for key in sorted(keys, key=delete_sort_key, reverse=True):
        response, _ = await _s3_request_full(context, 'DELETE', '/' + key.key, {}, {},
                                             b'', _hash(b''))
        response.raise_for_status()


async def _list(context, path):
    key_prefix = \
        '' if path == PurePosixPath('.') else \
        path.as_posix() + S3_DIR_SUFFIX

    for child_path in await _list_immediate_child_paths(context, key_prefix):
        yield child_path


async def _unlink(context, path):
    key = path.as_posix()
    response, _ = await _s3_request_full(context, 'DELETE', '/' + key, {}, {}, b'', _hash(b''))
    response.raise_for_status()


def _open_wb(context, lock, path):
    key = path.as_posix()

    upload_id = None
    part_uploads = []

    part_length = None
    part_chunks = None
    part_payload_hash = None

    async def start():
        nonlocal upload_id
        new_part_init()
        upload_id = await _multipart_upload_start(context, key)

    def new_part_init():
        nonlocal part_length
        nonlocal part_chunks
        nonlocal part_payload_hash
        part_length = 0
        part_chunks = []
        part_payload_hash = hashlib.sha256()

    async def write(chunk):
        # If ingress is faster than egress, need to do something to avoid storing
        # the entire file in memory. Could have something better/fancier but in
        # our case, suspect egress to S3 will always be faster than ingress, so
        # we keep this simple and just prevent from running out of memory in case
        # it happens, but also prevent the connection from dying
        if len(part_uploads) > 2 and not part_uploads[-2].done():
            await asyncio.sleep(MULTIPART_UPLOAD_IF_SLOW_SLEEP_SECONDS)

            in_progress = [upload for upload in part_uploads if not upload.done()]
            if len(in_progress) > MULTIPART_UPLOAD_MAX_CONCURRENT_UPLOADS_PER_FILE:
                raise Exception('Too many incomplete uploads to S3')

        nonlocal part_length
        part_length += len(chunk)
        part_chunks.append(chunk)
        part_payload_hash.update(chunk)

        if part_length >= MULTIPART_UPLOAD_MIN_BYTES:
            upload_part()

    def upload_part():
        part_number = str(len(part_uploads) + 1)
        upload_coro = _multipart_upload_part(
            context, key, upload_id,
            part_number, part_length, part_chunks, part_payload_hash.hexdigest())
        part_uploads.append(asyncio.create_task(upload_coro))
        new_part_init()

    async def end():
        # AWS throws an error if the multipart upload doesn't have at least one part
        # which would happen if write is never called, i.e. for an empty file
        if not part_uploads:
            await write(b'')

        if part_chunks:
            upload_part()

        indexes_and_etags = await asyncio.gather(*part_uploads)

        # Completing the upload is the only action that changes anything (apart from
        # APIs that query multipart uploads), so this all we need to wrap in a lock.
        # A bucket lifecycle policy can cleanup unfinished multipart uploads, so we
        # don't need to do it here
        async with lock(write_to=[path], read_from=[]):
            if await _is_file(context, path.parent):
                raise Exception('{} is not a directory'.format(path.parent))

            if not await _is_dir(context, path.parent):
                raise Exception('{} does not exist'.format(path.parent))

            # Overwrites are allowed, so there is no need to check if the file aleady exists

            await _multipart_upload_complete(context, key, upload_id, indexes_and_etags)

        new_part_init()

    class WritableFile():

        async def __aenter__(self):
            await start()

        async def __aexit__(self, exc_type, exc, traceback):
            if exc_type is None:
                await end()

        @staticmethod
        async def write(chunk):
            await write(chunk)

    return WritableFile()


async def _multipart_upload_start(context, key):
    query = {
        'uploads': '',
    }
    response, body = await _s3_request_full(context, 'POST', '/' + key, query, {},
                                            b'', _hash(b''))
    response.raise_for_status()
    return re.search(b'<UploadId>(.*)</UploadId>', body)[1].decode('utf-8')


async def _multipart_upload_part(context, key, upload_id, part_number, part_length,
                                 part_chunks, part_payload_hash):
    async def aiter(iterable):
        for item in iterable:
            await asyncio.sleep(0)
            yield item

    part_payload = aiter(part_chunks)
    query = {
        'partNumber': part_number,
        'uploadId': upload_id,
    }
    headers = {'Content-Length': str(part_length)}
    response, _ = await _s3_request_full(context, 'PUT', '/' + key, query, headers,
                                         part_payload, part_payload_hash)
    response.raise_for_status()
    part_etag = response.headers['ETag']

    return (part_number, part_etag)


async def _multipart_upload_complete(context, key, upload_id, part_numbers_and_etags):

    payload = (
        '<CompleteMultipartUpload>' +
        ''.join(
            f'<Part><PartNumber>{part_number}</PartNumber><ETag>{part_etag}</ETag></Part>'
            for part_number, part_etag in part_numbers_and_etags
        ) +
        '</CompleteMultipartUpload>'
    ).encode('utf-8')
    payload_hash = _hash(payload)
    query = {
        'uploadId': upload_id,
    }
    response, _ = await _s3_request_full(context, 'POST', '/' + key, query, {},
                                         payload, payload_hash)
    response.raise_for_status()


def _open_rb(context, _, path):
    # S3 GETs are atomic, so we don't need any additional locking
    # It will either fail with a 404, or return a consistent object

    async def iter_data(count):
        key = path.as_posix()
        async with await _s3_request(context, 'GET', '/' + key, {}, {},
                                     b'', _hash(b'')) as response:
            response.raise_for_status()
            async for data in response.content.iter_chunked(count):
                yield data

    class ReadableFile():

        async def __aenter__(self):
            pass

        async def __aexit__(self, exc_type, exc, traceback):
            pass

        @staticmethod
        def iter_by_block(count):
            return iter_data(count)

    return ReadableFile()


async def _list_immediate_child_paths(context, key_prefix):
    list_keys, list_prefixes = await _list_keys(context, key_prefix, S3_DIR_SUFFIX)

    return [
        S3Path(list_key.key, stat=Stat(
            st_size=list_key.size,
            st_mtime=list_key.last_modified,
            st_ctime=list_key.last_modified,
            st_nlink=1,
            st_mode=REG_MODE,
        ))
        for list_key in list_keys
        if list_key.key[-1] != S3_DIR_SUFFIX
    ] + [
        S3Path(list_prefix.prefix, stat=Stat(
            # Not completely sure what size should be for a directory
            st_size=0,
            # Can't quite work out an efficient way of working out
            # any sort of meaningful modification/creation time for a
            # directory
            st_mtime=0,
            st_ctime=0,
            st_nlink=1,
            st_mode=DIR_MODE,
        ))
        for list_prefix in list_prefixes
    ]


async def _list_descendant_keys(context, key_prefix):
    list_keys, _ = await _list_keys(context, key_prefix, '')
    return list_keys


async def _list_keys(context, key_prefix, delimeter):
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
        response, body = await _s3_request_full(context, 'GET', '/', query, {}, b'', _hash(b''))
        response.raise_for_status()
        return _parse_list_response(body)

    async def _list_later_page(token):
        query = {
            **common_query,
            'continuation-token': token,
        }
        response, body = await _s3_request_full(context, 'GET', '/', query, {}, b'', _hash(b''))
        response.raise_for_status()
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
        keys = []
        prefixes = []
        for element in root:
            if element.tag == f'{namespace}Contents':
                key = _first_child_text(element, f'{namespace}Key')
                last_modified_str = _first_child_text(element, f'{namespace}LastModified')
                last_modified_datetime = datetime.strptime(
                    last_modified_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                last_modified_seconds = int(
                    (last_modified_datetime - epoch).total_seconds())
                size = int(_first_child_text(element, f'{namespace}Size'))
                keys.append(ListKey(key=key, last_modified=last_modified_seconds, size=size))

            if element.tag == f'{namespace}CommonPrefixes':
                # Prefixes end in '/', which we strip off
                prefix = _first_child_text(element, f'{namespace}Prefix')[:-1]
                prefixes.append(ListPrefix(prefix=prefix))

            if element.tag == f'{namespace}NextContinuationToken':
                next_token = element.text

        return (next_token, keys, prefixes)

    token, keys, prefixes = await _list_first_page()
    while token:
        token, keys_page, prefixes_page = await _list_later_page(token)
        keys.extend(keys_page)
        prefixes.extend(prefixes_page)

    return keys, prefixes


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


async def _s3_request_full(context, method, path, query, api_pre_auth_headers,
                           payload, payload_hash):
    async with await _s3_request(context, method, path, query, api_pre_auth_headers,
                                 payload, payload_hash) as result:
        return result, await result.read()


async def _s3_request(context, method, path, query, api_pre_auth_headers, payload, payload_hash):
    service = 's3'
    creds = await context.credentials()
    pre_auth_headers = {
        **api_pre_auth_headers,
        **creds.pre_auth_headers,
    }
    bucket = context.bucket
    full_path = f'/{bucket.name}{path}'

    headers = _aws_sig_v4_headers(
        creds.access_key_id, creds.secret_access_key, pre_auth_headers,
        service, bucket.region, bucket.host, method, full_path, query, payload_hash,
    )

    querystring = urllib.parse.urlencode(query, safe='~', quote_via=urllib.parse.quote)
    encoded_path = urllib.parse.quote(full_path, safe='/~')
    url = f'https://{bucket.host}{encoded_path}' + \
          (('?' + querystring) if querystring else '')

    return context.session.request(method, url, ssl=bucket.verify_certs,
                                   headers=headers, data=payload)


def _aws_sig_v4_headers(access_key_id, secret_access_key, pre_auth_headers,
                        service, region, host, method, path, query, payload_hash):
    algorithm = 'AWS4-HMAC-SHA256'

    now = datetime.utcnow()
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
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


OPENERS = {
    'wb': _open_wb,
    'rb': _open_rb,
}


class _ReadWaiter(asyncio.Future):
    pass


class _WriteWaiter(asyncio.Future):
    pass


class _ReadWriteLock():
    # Locks are acquired strictly in the order requested. This is different
    # to aio-libs/aiorwlock, which does not allow readers to acquire if
    # there are any waiting writers, which can starve readers

    def __init__(self):
        self._waiters = deque()
        self._reads_held = 0
        self._write_held = False

    def _pop_queued_waiters(self, waiter_type):
        while True:
            correct_type = self._waiters and isinstance(self._waiters[0], waiter_type)
            cancelled = self._waiters and self._waiters[0].cancelled()

            if correct_type or cancelled:
                waiter = self._waiters.popleft()

            if correct_type and not cancelled:
                yield waiter

            if not correct_type and not cancelled:
                break

    def _resolve_queued_waiters(self):
        if not self._write_held:
            for waiter in self._pop_queued_waiters(_ReadWaiter):
                self._reads_held += 1
                waiter.set_result(None)

        if not self._write_held and not self._reads_held:
            for waiter in self._pop_queued_waiters(_WriteWaiter):
                self._write_held = True
                waiter.set_result(None)
                break

    def _on_read_release(self):
        self._reads_held -= 1

    def _on_write_release(self):
        self._write_held = False

    @asynccontextmanager
    async def _acquire(self, waiter_type, on_release):
        waiter = waiter_type()
        self._waiters.append(waiter)
        self._resolve_queued_waiters()

        try:
            await waiter
        except asyncio.CancelledError:
            self._resolve_queued_waiters()
            raise

        try:
            yield
        finally:
            on_release()
            self._resolve_queued_waiters()

    @asynccontextmanager
    async def read(self):
        async with self._acquire(_ReadWaiter, self._on_read_release):
            yield

    @asynccontextmanager
    async def write(self):
        async with self._acquire(_WriteWaiter, self._on_write_release):
            yield


class _PathLock():

    # https://people.eecs.berkeley.edu/~kubitron/courses/cs262a-F14/projects/reports/project6_report.pdf
    #
    # Inspired by ^ but a simpler version without the distinction between
    # "path" and "data" locks: I couldn't see the benefit of that

    def __init__(self):
        self._locks = weakref.WeakValueDictionary()

    @staticmethod
    def _sort_key(path_lock):
        return len(path_lock[0].parents), path_lock[0].as_posix()

    def _with_locks(self, paths, locker):
        return [
            (path, self._locks.setdefault(path, default=_ReadWriteLock()), locker)
            for path in paths
        ]

    @asynccontextmanager
    async def __call__(self, write_to, read_from):
        writable_paths = set(write_to)
        writable_locks = self._with_locks(writable_paths, lambda lock: lock.write())

        ancestor_paths = _flatten(path.parents for path in write_to + read_from)
        readable_paths = set(ancestor_paths + read_from) - writable_paths
        readable_locks = self._with_locks(readable_paths, lambda lock: lock.read())

        sorted_locks = sorted(readable_locks + writable_locks, key=self._sort_key)
        async with AsyncExitStack() as stack:
            for _, lock, lock_func in sorted_locks:
                await stack.enter_async_context(lock_func(lock))

            yield


def _flatten(to_flatten):
    return [
        item
        for sub_list in to_flatten
        for item in sub_list
    ]
