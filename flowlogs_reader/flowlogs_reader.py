#  Copyright 2015 Observable Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import division, print_function

from calendar import timegm
from datetime import datetime, timedelta
from gzip import GzipFile
from io import BytesIO
from os.path import basename

import boto3
from botocore.exceptions import NoRegionError, PaginationError
from dateutil.rrule import rrule, DAILY

DEFAULT_FILTER_PATTERN = (
    '[version="2", account_id, interface_id, srcaddr, dstaddr, '
    'srcport, dstport, protocol, packets, bytes, '
    'start, end, action, log_status]'
)
DEFAULT_REGION_NAME = 'us-east-1'
DUPLICATE_NEXT_TOKEN_MESSAGE = 'The same next token was received twice'

ACCEPT = 'ACCEPT'
REJECT = 'REJECT'
SKIPDATA = 'SKIPDATA'
NODATA = 'NODATA'


class FlowRecord(object):
    """
    Given a VPC Flow Logs event dictionary, returns a Python object whose
    attributes match the field names in the event record. Integers are stored
    as Python int objects; timestamps are stored as Python datetime objects.
    """
    __slots__ = [
        'version',
        'account_id',
        'interface_id',
        'srcaddr',
        'dstaddr',
        'srcport',
        'dstport',
        'protocol',
        'packets',
        'bytes',
        'start',
        'end',
        'action',
        'log_status',
    ]

    def __init__(self, event, EPOCH_32_MAX=2147483647):
        fields = event['message'].split()
        self.version = int(fields[0])
        self.account_id = fields[1]
        self.interface_id = fields[2]

        # Contra the docs, the start and end fields can contain
        # millisecond-based timestamps.
        # http://docs.aws.amazon.com/AmazonVPC/latest/UserGuide/flow-logs.html
        start = int(fields[10])
        if start > EPOCH_32_MAX:
            start /= 1000

        end = int(fields[11])
        if end > EPOCH_32_MAX:
            end /= 1000

        self.start = datetime.utcfromtimestamp(start)
        self.end = datetime.utcfromtimestamp(end)

        self.log_status = fields[13]
        if self.log_status in (NODATA, SKIPDATA):
            self.srcaddr = None
            self.dstaddr = None
            self.srcport = None
            self.dstport = None
            self.protocol = None
            self.packets = None
            self.bytes = None
            self.action = None
        else:
            self.srcaddr = fields[3]
            self.dstaddr = fields[4]
            self.srcport = int(fields[5])
            self.dstport = int(fields[6])
            self.protocol = int(fields[7])
            self.packets = int(fields[8])
            self.bytes = int(fields[9])
            self.action = fields[12]

    def __eq__(self, other):
        try:
            return all(
                getattr(self, x) == getattr(other, x) for x in self.__slots__
            )
        except AttributeError:
            return False

    def __hash__(self):
        return hash(tuple(getattr(self, x) for x in self.__slots__))

    def __str__(self):
        ret = ['{}: {}'.format(x, getattr(self, x)) for x in self.__slots__]
        return ', '.join(ret)

    def to_dict(self):
        return {x: getattr(self, x) for x in self.__slots__}

    def to_message(self):
        D_transform = {
            'start': lambda dt: str(timegm(dt.utctimetuple())),
            'end': lambda dt: str(timegm(dt.utctimetuple())),
        }

        ret = []
        for attr in self.__slots__:
            transform = D_transform.get(attr, lambda x: str(x) if x else '-')
            ret.append(transform(getattr(self, attr)))

        return ' '.join(ret)

    @classmethod
    def from_message(cls, message):
        return cls({'message': message})


class BaseReader(object):
    def __init__(
        self,
        client_type,
        region_name=None,
        profile_name=None,
        start_time=None,
        end_time=None,
        boto_client_kwargs=None,
        boto_client=None,
    ):
        # Get a boto3 client with which to perform queries
        if boto_client is not None:
            self.boto_client = boto_client
        else:
            self.boto_client = self._get_client(
                client_type, region_name, profile_name, boto_client_kwargs
            )

        # If no time filters are given use the last hour
        now = datetime.utcnow()
        self.start_time = start_time or now - timedelta(hours=1)
        self.end_time = end_time or now

        # Initialize the iterator
        self.iterator = self._reader()

    def _get_client(
        self, client_type, region_name, profile_name, boto_client_kwargs
    ):
        session_kwargs = {}
        if region_name is not None:
            session_kwargs['region_name'] = region_name

        if profile_name is not None:
            session_kwargs['profile_name'] = profile_name

        client_kwargs = boto_client_kwargs or {}

        session = boto3.session.Session(**session_kwargs)
        try:
            boto_client = session.client(client_type, **client_kwargs)
        except NoRegionError:
            boto_client = session.client(
                client_type, region_name=DEFAULT_REGION_NAME, **client_kwargs
            )

        return boto_client

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def next(self):
        # For Python 2 compatibility
        return self.__next__()

    def _reader(self):
        # Loops through each log stream and its events, yielding a parsed
        # version of each event.
        for event in self._read_streams():
            yield FlowRecord(event)


class FlowLogsReader(BaseReader):
    """
    Returns an object that will yield VPC Flow Log records as Python objects.
    * `log_group_name` is the name of the CloudWatch Logs group that stores
    your VPC flow logs.
    * `region_name` is the AWS region.
    * `profile_name` is the AWS boto3 configuration profile to use.
    * `start_time` is a Python datetime.datetime object; only the log events
    from at or after this time will be considered.
    * `end_time` is a Python datetime.datetime object; only the log events
    before this time will be considered.
    * `filter_pattern` is a string passed to CloudWatch as a filter pattern
    * `boto_client_kwargs` - keyword arguments to pass to the boto3 client
    * `boto_client` - your own boto3 client object. If given then region_name,
    profile_name, and boto_client_kwargs will be ignored.
    """

    def __init__(
        self, log_group_name, filter_pattern=DEFAULT_FILTER_PATTERN, **kwargs
    ):
        super(FlowLogsReader, self).__init__('logs', **kwargs)
        self.log_group_name = log_group_name

        self.paginator_kwargs = {}

        if filter_pattern is not None:
            self.paginator_kwargs['filterPattern'] = filter_pattern

        self.start_ms = timegm(self.start_time.utctimetuple()) * 1000
        self.end_ms = timegm(self.end_time.utctimetuple()) * 1000

    def _read_streams(self):
        paginator = self.boto_client.get_paginator('filter_log_events')
        response_iterator = paginator.paginate(
            logGroupName=self.log_group_name,
            startTime=self.start_ms,
            endTime=self.end_ms,
            interleaved=True,
            **self.paginator_kwargs
        )

        try:
            for page in response_iterator:
                for event in page['events']:
                    yield event
        except PaginationError as e:
            if e.kwargs['message'].startswith(DUPLICATE_NEXT_TOKEN_MESSAGE):
                pass
            else:
                raise


class S3FlowLogsReader(BaseReader):
    def __init__(
        self,
        location,
        include_accounts=None,
        include_regions=None,
        **kwargs
    ):
        super(S3FlowLogsReader, self).__init__('s3', **kwargs)

        location_parts = (location.rstrip('/') + '/').split('/', 1)
        self.bucket, self.prefix = location_parts

        self.include_accounts = (
            None if include_accounts is None else set(include_accounts)
        )
        self.include_regions = (
            None if include_regions is None else set(include_regions)
        )

    def _read_file(self, key):
        resp = self.boto_client.get_object(Bucket=self.bucket, Key=key)
        with BytesIO(resp['Body'].read()) as f:
            with GzipFile(fileobj=f, mode='rb') as gz_f:
                # Skip the header
                next(gz_f)

                # Yield the rest of the lines
                for line in gz_f:
                    yield line.decode('utf-8')

    def _get_keys(self, prefix):
        # S3 keys have a file name like:
        # account_vpcflowlogs_region_flow-logs-id_datetime_hash.log.gz
        # Yield the keys for files relevant to our time range
        paginator = self.boto_client.get_paginator('list_objects_v2')
        all_pages = paginator.paginate(Bucket=self.bucket, Prefix=prefix)
        for page in all_pages:
            for item in page.get('Contents', []):
                key = item['Key']
                file_name = basename(key)
                try:
                    dt = datetime.strptime(
                        file_name.rsplit('_', 2)[1], '%Y%m%dT%H%MZ'
                    )
                except (IndexError, ValueError):
                    continue

                if self.start_time <= dt < self.end_time:
                    yield key

    def _get_date_prefixes(self):
        # Each base_location/AWSLogs/account_number/vpcflowlogs/region_name/
        # prefix has files organized in year/month/day directories.
        # Yield the year/month/day/ fragments that are relevant to our
        # time range
        dtstart = self.start_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        until = self.end_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for dt in rrule(freq=DAILY, dtstart=dtstart, until=until):
            yield dt.strftime('%Y/%m/%d/')

    def _get_region_prefixes(self, account_prefix):
        # Yield each prefix of the type:
        # base_location/AWSLogs/account_number/vpcflowlogs/region_name/
        resp = self.boto_client.list_objects_v2(
            Bucket=self.bucket,
            Delimiter='/',
            Prefix=account_prefix + 'vpcflowlogs/'
        )
        for item in resp.get('CommonPrefixes', []):
            prefix = item['Prefix']
            if self.include_regions is not None:
                region_name = prefix.rsplit('/', 2)[1]
                if region_name not in self.include_regions:
                    continue

            yield prefix

    def _get_account_prefixes(self):
        # Yield each prefix of the type:
        # base_location/AWSLogs/account_number/
        prefix = self.prefix.strip('/') + '/AWSLogs/'
        prefix = prefix.lstrip('/')
        resp = self.boto_client.list_objects_v2(
            Bucket=self.bucket, Delimiter='/', Prefix=prefix
        )
        for item in resp.get('CommonPrefixes', []):
            prefix = item['Prefix']
            if self.include_accounts is not None:
                account_id = prefix.rsplit('/', 2)[1]
                if account_id not in self.include_accounts:
                    continue

            yield prefix

    def _read_streams(self):
        for account_prefix in self._get_account_prefixes():
            for region_prefix in self._get_region_prefixes(account_prefix):
                for day_prefix in self._get_date_prefixes():
                    prefix = region_prefix + day_prefix
                    for key in self._get_keys(prefix):
                        for message in self._read_file(key):
                            yield {'message': message}
