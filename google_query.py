"""Google browser-compatible query envelope, layered on the pinned fast-flights schema.

Additional fields were observed in Google Flights UI-generated URLs on 2026-10-18.
An isolated descriptor pool avoids modifying the dependency or its global schema.
Existing passenger, trip, stop and transfer-filter fields are preserved by parsing
its original serialized message before adding the browser envelope.
"""
from __future__ import annotations

import base64
from dataclasses import fields
from functools import lru_cache
import json
from pathlib import Path
from urllib.parse import urlencode

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from fast_flights.pb.flights_pb2 import Info
from fast_flights.querying import Query

ENCODING_VERSION = "browser_v1"
BROWSER_TFU = "EgYIABAAGAA"


def _browser_message_class():
    descriptor = descriptor_pb2.FileDescriptorProto()
    Info.DESCRIPTOR.file.CopyToProto(descriptor)
    messages = {message.name: message for message in descriptor.message_type}

    def add_field(message, name, number, field_type, type_name=None):
        field = message.field.add(name=name, number=number, type=field_type,
                                  label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL)
        if type_name:
            field.type_name = type_name

    # Native URLs encode repeated passengers as individual varints, not packed.
    next(field for field in messages['Info'].field if field.name == 'passengers').options.packed = False
    types = descriptor_pb2.FieldDescriptorProto
    add_field(messages['Airport'], 'entity_type', 1, types.TYPE_INT32)
    add_field(messages['Info'], 'browser_source', 1, types.TYPE_INT32)
    add_field(messages['Info'], 'results_mode', 2, types.TYPE_INT32)
    add_field(messages['Info'], 'display_settings', 14, types.TYPE_INT32)
    all_results = descriptor.message_type.add(name='BrowserAllResults')
    add_field(all_results, 'value', 1, types.TYPE_INT64)
    add_field(messages['Info'], 'all_results', 16, types.TYPE_MESSAGE, '.BrowserAllResults')
    pool = descriptor_pool.DescriptorPool()
    pool.Add(descriptor)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName('Info'))


BrowserInfo = _browser_message_class()


@lru_cache(maxsize=1)
def city_entities():
    with (Path(__file__).parent / 'data' / 'google-city-entities.json').open() as source:
        return json.load(source)['entities']


class BrowserQuery(Query):
    """Keep fast-flights' public query interface while matching browser wire fields."""

    def pb(self):
        message = BrowserInfo.FromString(super().pb().SerializeToString())
        message.browser_source = 28
        message.results_mode = 2
        message.display_settings = 1
        message.all_results.value = -1
        for flight in message.data:
            for location in (flight.from_airport, flight.to_airport):
                entity = city_entities().get(location.airport)
                if entity:
                    location.airport = entity['id']
                    location.entity_type = entity['type']
                else:
                    # Keep native IATA handling where a Google city ID is unverified.
                    location.entity_type = 1
        return message

    def to_str(self):
        return base64.urlsafe_b64encode(self.to_bytes()).rstrip(b'=').decode('ascii')

    def params(self):
        return {'tfs': self.to_str(), 'tfu': BROWSER_TFU,
                'hl': self.language, 'curr': self.currency}

    def url(self):
        return 'https://www.google.com/travel/flights/search?' + urlencode(self.params())


def with_browser_encoding(query: Query) -> BrowserQuery:
    return BrowserQuery(**{field.name: getattr(query, field.name) for field in fields(Query)})
