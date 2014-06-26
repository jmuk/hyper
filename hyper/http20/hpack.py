# -*- coding: utf-8 -*-
"""
hyper/http20/hpack
~~~~~~~~~~~~~~~~~~

Implements the HPACK header compression algorithm as detailed by the IETF.

Implements the version dated January 9, 2014.
"""
import collections
import logging

from ..compat import to_byte
from .huffman import HuffmanDecoder, HuffmanEncoder
from .hpack_structures import Reference
from hyper.http20.huffman_constants import (
    REQUEST_CODES, REQUEST_CODES_LENGTH, REQUEST_CODES, REQUEST_CODES_LENGTH
)
from .exceptions import HPACKEncodingError

log = logging.getLogger(__name__)

# The implementation draft of HPACK we support.
DRAFT = 7


def encode_integer(integer, prefix_bits):
    """
    This encodes an integer according to the wacky integer encoding rules
    defined in the HPACK spec.
    """
    log.debug("Encoding %d with %d bits.", integer, prefix_bits)

    max_number = (2 ** prefix_bits) - 1

    if (integer < max_number):
        return bytearray([integer])  # Seriously?
    else:
        elements = [max_number]
        integer = integer - max_number

        while integer >= 128:
            elements.append((integer % 128) + 128)
            integer = integer // 128  # We need integer division

        elements.append(integer)

        return bytearray(elements)


def decode_integer(data, prefix_bits):
    """
    This decodes an integer according to the wacky integer encoding rules
    defined in the HPACK spec. Returns a tuple of the decoded integer and the
    number of bytes that were consumed from ``data`` in order to get that
    integer.
    """
    multiple = lambda index: 128 ** (index - 1)
    max_number = (2 ** prefix_bits) - 1
    mask = 0xFF >> (8 - prefix_bits)
    index = 0

    number = to_byte(data[index]) & mask

    if (number == max_number):

        while True:
            index += 1
            next_byte = to_byte(data[index])

            if next_byte >= 128:
                number += (next_byte - 128) * multiple(index)
            else:
                number += next_byte * multiple(index)
                break

    log.debug("Decoded %d consuming %d bytes.", number, index + 1)

    return (number, index + 1)


def _to_bytes(string):
    """
    Convert string to bytes.
    """
    if not isinstance(string, (str, bytes)):
        string = str(string)

    return string if isinstance(string, bytes) else string.encode('utf-8')


def header_table_size(table):
    """
    Calculates the 'size' of the header table as defined by the HTTP/2
    specification.
    """
    # It's phenomenally frustrating that the specification feels it is able to
    # tell me how large the header table is, considering that its calculations
    # assume a very particular layout that most implementations will not have.
    # I appreciate it's an attempt to prevent DoS attacks by sending lots of
    # large headers in the header table, but it seems like a better approach
    # would be to limit the size of headers. Ah well.
    return sum(32 + len(name) + len(value) for name, value in table)


class Encoder(object):
    """
    An HPACK encoder object. This object takes HTTP headers and emits encoded
    HTTP/2 header blocks.
    """
    # This is the static table of header fields.
    static_table = [
        (b':authority', b''),
        (b':method', b'GET'),
        (b':method', b'POST'),
        (b':path', b'/'),
        (b':path', b'/index.html'),
        (b':scheme', b'http'),
        (b':scheme', b'https'),
        (b':status', b'200'),
        (b':status', b'204'),
        (b':status', b'206'),
        (b':status', b'304'),
        (b':status', b'400'),
        (b':status', b'404'),
        (b':status', b'500'),
        (b'accept-charset', b''),
        (b'accept-encoding', b'gzip, deflate'),
        (b'accept-language', b''),
        (b'accept-ranges', b''),
        (b'accept', b''),
        (b'access-control-allow-origin', b''),
        (b'age', b''),
        (b'allow', b''),
        (b'authorization', b''),
        (b'cache-control', b''),
        (b'content-disposition', b''),
        (b'content-encoding', b''),
        (b'content-language', b''),
        (b'content-length', b''),
        (b'content-location', b''),
        (b'content-range', b''),
        (b'content-type', b''),
        (b'cookie', b''),
        (b'date', b''),
        (b'etag', b''),
        (b'expect', b''),
        (b'expires', b''),
        (b'from', b''),
        (b'host', b''),
        (b'if-match', b''),
        (b'if-modified-since', b''),
        (b'if-none-match', b''),
        (b'if-range', b''),
        (b'if-unmodified-since', b''),
        (b'last-modified', b''),
        (b'link', b''),
        (b'location', b''),
        (b'max-forwards', b''),
        (b'proxy-authenticate', b''),
        (b'proxy-authorization', b''),
        (b'range', b''),
        (b'referer', b''),
        (b'refresh', b''),
        (b'retry-after', b''),
        (b'server', b''),
        (b'set-cookie', b''),
        (b'strict-transport-security', b''),
        (b'transfer-encoding', b''),
        (b'user-agent', b''),
        (b'vary', b''),
        (b'via', b''),
        (b'www-authenticate', b''),
    ]

    def __init__(self):
        self.header_table = collections.deque()
        self._header_table_size = 4096  # This value set by the standard.
        self.huffman_coder = HuffmanEncoder(
            REQUEST_CODES, REQUEST_CODES_LENGTH
        )

        # Confusingly, the reference set is a dictionary. This is because we
        # want to be able to get at the individual references, rather than just
        # test for presence.
        self.reference_set = {}

    @property
    def header_table_size(self):
        return self._header_table_size

    @header_table_size.setter
    def header_table_size(self, value):
        log.debug(
            "Setting header table size to %d from %d",
            value,
            self._header_table_size
        )

        # If the new value is larger than the current one, no worries!
        # Otherwise, we may need to shrink the header table.
        if value < self._header_table_size:
            current_size = header_table_size(self.header_table)

            while value < current_size:
                header = self.header_table.pop()
                n, v = header
                current_size -= (
                    32 + len(n) + len(v)
                )

                # If something is removed from the header table, it also needs
                # to be removed from the reference set.
                try:
                    del self.reference_set[Reference(header)]
                except KeyError:
                    pass

                log.debug(
                    "Removed %s: %s from the encoder header table", n, v
                )

        self._header_table_size = value

    def encode(self, headers, huffman=True):
        """
        Takes a set of headers and encodes them into a HPACK-encoded header
        block.

        Transforming the headers into a header block is a procedure that can
        be modeled as a chain or pipe. First, the headers are compared against
        the reference set. Any headers already in the reference set don't need
        to be emitted at all, they can be left alone. Headers not in the
        reference set need to be emitted. Headers in the reference set that
        need to be removed (potentially to be replaced) need to be emitted as
        well.

        Next, the headers are encoded. This encoding can be done a number of
        ways. If the header name-value pair are already in the header table we
        can represent them using the indexed representation: the same is true
        if they are in the static table. Otherwise, a literal representation
        will be used.

        Literal text values may optionally be Huffman encoded. For now we don't
        do that, because it's an extra bit of complication, but we will later.
        """
        log.debug("HPACK encoding %s", headers)
        header_block = []

        # A preliminary step will unmark all the references.
        for ref in self.reference_set:
            ref.emitted = Reference.NOT_EMITTED

        # Turn the headers into a list of tuples if possible. This is the
        # natural way to interact with them in HPACK.
        if isinstance(headers, dict):
            headers = headers.items()

        # Next, walk across the headers and turn them all into bytestrings.
        headers = [(_to_bytes(n), _to_bytes(v)) for n, v in headers]

        # We can now encode each header in the block. The logic here roughly
        # goes as follows:
        # 1. Check whether the header is in the reference set. If it is and
        #    hasn't been emitted yet, mark it as emitted. If it has been
        #    emitted, do the crazy unemit-reemit dance.
        # 2. If the header is not in the reference set, emit it and add it to
        #    the reference set as an emitted header.
        # 3. When we're done with the header block, explicitly remove all
        #    unemitted references.
        for header in headers:
            # Search for the header in the header table.
            m = self.matching_header(*header)

            if m is not None:
                index, match = m
            else:
                index, match = -1, None

            # Found it. Is it in the reference set?
            ref = self.get_from_reference_set(match)

            if ref is not None and not ref.emitted:
                # Mark it as emitted.
                ref.emitted = Reference.IMPLICITLY_EMITTED
                continue

            if ref is not None:
                # Already emitted, emit again. This requires a strange dance of
                # removal and then re-emission. To do this with minimal code,
                # we set ref back to None in this block so that we'll fall
                # into the next branch.
                if ref.emitted == Reference.IMPLICITLY_EMITTED:
                    # We actually need to do this twice becuase of the implicit
                    # emission.
                    header_block.append(self.remove(ref.obj))
                    header_block.append(self.add(header))
                    header = self.matching_header(*header)[1]
                    ref = self.get_from_reference_set(header)

                header_block.append(self.remove(ref.obj))
                ref = None

            if ref is None:
                # Not in the reference set, emit and add.
                header_block.append(self.add(header, huffman))

        # Remove everything we didn't emit. We do this in a specific order so
        # that we generate deterministic output, even at the cost of being
        # slower.
        for r in sorted(self.reference_set.keys(), key=lambda r: r.obj):
            if not r.emitted:
                header_block.append(self.remove(r.obj))

        log.debug("Encoded header block to %s", header_block)

        return b''.join(header_block)

    def remove(self, header):
        """
        This function takes a header key-value tuple and serializes it.  It
        must be in the header table, so must be represented in its indexed
        form.
        """
        log.debug(
            "Removing %s:%s from the reference set", header[0], header[1]
        )

        try:
            index, match = self.matching_header(*header)
        except TypeError:
            raise HPACKEncodingError(
                '"%s: %s" not present in the header table' %
                (header[0], header[1])
            )

        # The header must be in the header block. That means that:
        # - match must be the header tuple
        # - index must be <= len(self.header_table)
        max_index = len(self.header_table)

        if (not match) or (index > max_index):
            raise HPACKEncodingError(
                '"%s: %s" not present in the header table' %
                (header[0], header[1])
            )

        # We can safely encode this as the indexed representation.
        encoded = self._encode_indexed(index)

        # Having encoded it in the indexed form, we now remove it from the
        # reference set.
        del self.reference_set[Reference(header)]

        return encoded

    def add(self, to_add, huffman=False):
        """
        This function takes a header key-value tuple and serializes it for
        adding to the header table.
        """
        log.debug("Adding %s to the header table", to_add)

        name, value = to_add

        # Search for a matching header in the header table.
        match = self.matching_header(name, value)

        if match is None:
            # Not in the header table. Encode using the literal syntax,
            # and add it to the header table.
            encoded = self._encode_literal(name, value, True, huffman)
            self._add_to_header_table(to_add)
            ref = Reference(to_add)
            ref.emitted = Reference.EMITTED
            self.reference_set[ref] = ref
            return encoded

        # The header is in the table, break out the values. If we matched
        # perfectly, we can use the indexed representation: otherwise we
        # can use the indexed literal.
        index, perfect = match

        if perfect:
            # Indexed representation. If the index is larger than the size
            # of the header table, also add to the header table.
            encoded = self._encode_indexed(index)

            if index > len(self.header_table):
                perfect = (name, value)
                self._add_to_header_table(perfect)

            ref = Reference(perfect)
            ref.emitted = Reference.EMITTED
            self.reference_set[ref] = ref
        else:
            # Indexed literal. Since we have a partial match, don't add to
            # the header table, it won't help us.
            encoded = self._encode_indexed_literal(index, value, huffman)

        return encoded

    def matching_header(self, name, value):
        """
        Scans the header table and the static table. Returns a tuple, where the
        first value is the index of the match, and the second is whether there
        was a full match or not. Prefers full matches to partial ones.

        Upsettingly, the header table is one-indexed, not zero-indexed.
        """
        partial_match = None
        header_table_size = len(self.header_table)

        for (i, (n, v)) in enumerate(self.header_table):
            if n == name:
                if v == value:
                    return (i + 1, self.header_table[i])
                elif partial_match is None:
                    partial_match = (i + 1, None)

        for (i, (n, v)) in enumerate(Encoder.static_table):
            if n == name:
                if v == value:
                    return (i + header_table_size + 1, Encoder.static_table[i])
                elif partial_match is None:
                    partial_match = (i + header_table_size + 1, None)

        return partial_match

    def get_from_reference_set(self, header):
        """
        Determines whether a header is currently in the reference set. Returns
        ``None`` if not.

        :param header: The header tuple to search for.
        """
        if header is None:
            return None

        r = Reference(header)
        try:
            return self.reference_set[r]
        except KeyError:
            return None

    def _add_to_header_table(self, header):
        """
        Adds a header to the header table, evicting old ones if necessary.
        """
        # Be optimistic: add the header straight away.
        self.header_table.appendleft(header)

        # Now, work out how big the header table is.
        actual_size = header_table_size(self.header_table)

        # Loop and remove whatever we need to.
        while actual_size > self.header_table_size:
            header = self.header_table.pop()
            n, v = header
            actual_size -= (
                32 + len(n) + len(v)
            )

            # If something is removed from the header table, it also needs to
            # be removed from the reference set.
            try:
                del self.reference_set[Reference(header)]
            except KeyError:
                pass

            log.debug("Evicted %s: %s from the header table", n, v)

    def _encode_indexed(self, index):
        """
        Encodes a header using the indexed representation.
        """
        field = encode_integer(index, 7)
        field[0] = field[0] | 0x80  # we set the top bit
        return bytes(field)

    def _encode_literal(self, name, value, indexing, huffman=False):
        """
        Encodes a header with a literal name and literal value. If ``indexing``
        is True, the header will be added to the header table: otherwise it
        will not.
        """
        prefix = b'\x40' if indexing else b'\x00'

        if huffman:
            name = self.huffman_coder.encode(name)
            value = self.huffman_coder.encode(value)

        name_len = encode_integer(len(name), 7)
        value_len = encode_integer(len(value), 7)

        if huffman:
            name_len[0] |= 0x80
            value_len[0] |= 0x80

        return b''.join([prefix, bytes(name_len), name, bytes(value_len), value])

    def _encode_indexed_literal(self, index, value, huffman=False):
        """
        Encodes a header with an indexed name and a literal value.
        """
        name = encode_integer(index, 4)

        if huffman:
            value = self.huffman_coder.encode(value)

        value_len = encode_integer(len(value), 7)

        if huffman:
            value_len[0] |= 0x80

        return b''.join([bytes(name), bytes(value_len), value])


class Decoder(object):
    """
    An HPACK decoder object.
    """
    static_table = [
        (b':authority', b''),
        (b':method', b'GET'),
        (b':method', b'POST'),
        (b':path', b'/'),
        (b':path', b'/index.html'),
        (b':scheme', b'http'),
        (b':scheme', b'https'),
        (b':status', b'200'),
        (b':status', b'204'),
        (b':status', b'206'),
        (b':status', b'304'),
        (b':status', b'400'),
        (b':status', b'404'),
        (b':status', b'500'),
        (b'accept-charset', b''),
        (b'accept-encoding', b'gzip, deflate'),
        (b'accept-language', b''),
        (b'accept-ranges', b''),
        (b'accept', b''),
        (b'access-control-allow-origin', b''),
        (b'age', b''),
        (b'allow', b''),
        (b'authorization', b''),
        (b'cache-control', b''),
        (b'content-disposition', b''),
        (b'content-encoding', b''),
        (b'content-language', b''),
        (b'content-length', b''),
        (b'content-location', b''),
        (b'content-range', b''),
        (b'content-type', b''),
        (b'cookie', b''),
        (b'date', b''),
        (b'etag', b''),
        (b'expect', b''),
        (b'expires', b''),
        (b'from', b''),
        (b'host', b''),
        (b'if-match', b''),
        (b'if-modified-since', b''),
        (b'if-none-match', b''),
        (b'if-range', b''),
        (b'if-unmodified-since', b''),
        (b'last-modified', b''),
        (b'link', b''),
        (b'location', b''),
        (b'max-forwards', b''),
        (b'proxy-authenticate', b''),
        (b'proxy-authorization', b''),
        (b'range', b''),
        (b'referer', b''),
        (b'refresh', b''),
        (b'retry-after', b''),
        (b'server', b''),
        (b'set-cookie', b''),
        (b'strict-transport-security', b''),
        (b'transfer-encoding', b''),
        (b'user-agent', b''),
        (b'vary', b''),
        (b'via', b''),
        (b'www-authenticate', b''),
    ]

    def __init__(self):
        self.header_table = collections.deque()
        self.reference_set = set()
        self._header_table_size = 4096  # This value set by the standard.
        self.huffman_coder = HuffmanDecoder(
            REQUEST_CODES, REQUEST_CODES_LENGTH
        )

    @property
    def header_table_size(self):
        return self._header_table_size

    @header_table_size.setter
    def header_table_size(self, value):
        log.debug(
            "Resizing decoder header table to %d from %d",
            value,
            self._header_table_size
        )

        # If the new value is larger than the current one, no worries!
        # Otherwise, we may need to shrink the header table.
        if value < self._header_table_size:
            current_size = header_table_size(self.header_table)

            while value < current_size:
                header = self.header_table.pop()
                n, v = header
                current_size -= (
                    32 + len(n) + len(v)
                )

                # If something is removed from the header table, it also needs
                # to be removed from the reference set.
                self.reference_set.discard(Reference(header))

                log.debug("Evicting %s: %s from the header table", n, v)

        self._header_table_size = value

    def decode(self, data):
        """
        Takes an HPACK-encoded header block and decodes it into a header set.
        """
        log.debug("Decoding %s", data)

        headers = []
        data_len = len(data)
        current_index = 0

        while current_index < data_len:
            # Work out what kind of header we're decoding.
            # If the high bit is 1, it's an indexed field.
            current = to_byte(data[current_index])
            indexed = bool(current & 0x80)

            # Otherwise, if the second-highest bit is 1 it's a field that does
            # alter the header table.
            literal_index = bool(current & 0x40)

            # Otherwise, if the third-highest bit is 1 it's an encoding context
            # update.
            encoding_update = bool(current & 0x20)

            if indexed:
                header, consumed = self._decode_indexed(data[current_index:])
            elif literal_index:
                # It's a literal header that does affect the header table.
                header, consumed = self._decode_literal_index(
                    data[current_index:]
                )
            elif encoding_update:
                # It's an update to the encoding context.
                consumed = self._update_encoding_context(data)
                header = None
            else:
                # It's a literal header that does not affect the header table.
                header, consumed = self._decode_literal_no_index(
                    data[current_index:]
                )

            if header:
                headers.append(header)

            current_index += consumed

        # Now we're at the end, anything in the reference set that isn't in the
        # headers already gets added. Right now this is a slow linear search,
        # but we can probably do better in future.
        for ref in self.reference_set:
            if ref.obj not in headers:
                headers.append(ref.obj)

        return [(n.decode('utf-8'), v.decode('utf-8')) for n, v in headers]

    def _add_to_header_table(self, new_header):
        """
        Adds a header to the header table, evicting old ones if necessary.
        """
        # Be optimistic: add the header straight away.
        self.header_table.appendleft(new_header)

        # Now, work out how big the header table is.
        actual_size = header_table_size(self.header_table)

        # Loop and remove whatever we need to.
        while actual_size > self.header_table_size:
            header = self.header_table.pop()
            n, v = header
            actual_size -= (
                32 + len(n) + len(v)
            )

            # If something is removed from the header table, it also needs to
            # be removed from the reference set.
            self.reference_set.discard(Reference(header))

            log.debug("Evicting %s: %s from the header table", n, v)

    def _update_encoding_context(self, data):
        """
        Handles a byte that updates the encoding context.
        """
        # If the byte is 0x30, this empties the reference set.
        if to_byte(data[0]) == 0x30:
            self.reference_set = set()
            consumed = 1
        else:
            # We've been asked to resize the header table.
            new_size, consumed = decode_integer(data, 4)
            self.header_table_size = new_size
        return consumed

    def _decode_indexed(self, data):
        """
        Decodes a header represented using the indexed representation.
        """
        index, consumed = decode_integer(data, 7)
        index -= 1  # Because this idiot table is 1-indexed. Ugh.

        if index > len(self.header_table):
            index -= len(self.header_table)
            header = Decoder.static_table[index]

            # If this came out of the static table, we need to add it to the
            # header table.
            self._add_to_header_table(header)
        else:
            header = self.header_table[index]

        # If the header is in the reference set, remove it. Otherwise, add it.
        # Since this updates the reference set, don't bother returning the
        # header.
        header_ref = Reference(header)
        if header_ref in self.reference_set:
            log.debug(
                "Removed %s from the reference set, consumed %d",
                header,
                consumed
            )
            self.reference_set.remove(header_ref)
            return None, consumed
        else:
            log.debug("Decoded %s, consumed %d", header, consumed)
            self.reference_set.add(header_ref)
            return header, consumed

    def _decode_literal_no_index(self, data):
        return self._decode_literal(data, False)

    def _decode_literal_index(self, data):
        return self._decode_literal(data, True)

    def _decode_literal(self, data, should_index):
        """
        Decodes a header represented with a literal.
        """
        total_consumed = 0

        # When should_index is true, if the low six bits of the first byte are
        # nonzero, the header name is indexed.
        # When should_index is false, if the first byte is nonzero the header
        # name is indexed.
        if should_index:
            indexed_name = to_byte(data[0]) & 0x3F
            name_len = 6
        else:
            indexed_name = to_byte(data[0])
            name_len = 4

        if indexed_name:
            # Indexed header name.
            index, consumed = decode_integer(data, name_len)
            index -= 1

            if index >= len(self.header_table):
                index -= len(self.header_table)
                name = Decoder.static_table[index][0]
            else:
                name = self.header_table[index][0]

            total_consumed = consumed
            length = 0
        else:
            # Literal header name. The first byte was consumed, so we need to
            # move forward.
            data = data[1:]

            length, consumed = decode_integer(data, 7)
            name = data[consumed:consumed + length]

            if to_byte(data[0]) & 0x80:
                name = self.huffman_coder.decode(name)
            total_consumed = consumed + length + 1  # Since we moved forward 1.

        data = data[consumed + length:]

        # The header value is definitely length-based.
        length, consumed = decode_integer(data, 7)
        value = data[consumed:consumed + length]

        if to_byte(data[0]) & 0x80:
            value = self.huffman_coder.decode(value)

        # Updated the total consumed length.
        total_consumed += length + consumed

        # If we've been asked to index this, add it to the header table and
        # the reference set.
        header = (name, value)
        if should_index:
            self._add_to_header_table(header)
            self.reference_set.add(Reference(header))

        log.debug(
            "Decoded %s, consumed %d, indexed %s",
            header,
            consumed,
            should_index
        )

        return header, total_consumed
