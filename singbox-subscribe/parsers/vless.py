from utils import tool
import re
from urllib.parse import urlparse, parse_qs, unquote

def parse(data):
    info = data[:]
    server_info = urlparse(info)
    try:
        netloc = tool.b64Decode(server_info.netloc).decode('utf-8')
    except:
        netloc = server_info.netloc

    _netloc = netloc.rsplit("@", 1)
    if len(_netloc) < 2:
        return None

    try:
        _netloc_parts = _netloc[1].rsplit(":", 1)
    except:
        return None

    if _netloc_parts[1].isdigit():
        server = re.sub(r"\[|\]", "", _netloc_parts[0])
        server_port = int(_netloc_parts[1])
    else:
        return None

    netquery = dict(
        (k, v if len(v) > 1 else v[0])
        for k, v in parse_qs(server_info.query).items()
    )

    if netquery.get('remarks'):
        remarks = netquery['remarks']
    else:
        remarks = server_info.fragment

    node = {
        'tag': unquote(remarks) or tool.genName()+'_vless',
        'type': 'vless',
        'server': server,
        'server_port': server_port,
        'uuid': _netloc[0].split(':', 1)[-1],
    }

    packet_encoding = netquery.get('packetEncoding')
    if isinstance(packet_encoding, str):
        packet_encoding = packet_encoding.strip()
    if packet_encoding and packet_encoding.lower() != 'none':
        node['packet_encoding'] = packet_encoding
    elif packet_encoding is None:
        node['packet_encoding'] = 'xudp'

    if netquery.get('flow'):
        node['flow'] = 'xtls-rprx-vision'

    # Настройки TLS / REALITY
    if netquery.get('security', '') not in ['None', 'none', ''] or netquery.get('tls') == '1':
        node['tls'] = {
            'enabled': True,
            'insecure': False,
            'server_name': ''
        }
        if netquery.get('allowInsecure') == '1':
            node['tls']['insecure'] = True

        node['tls']['server_name'] = netquery.get('sni', '') or netquery.get('peer', '')
        if node['tls']['server_name'] == 'None':
            node['tls']['server_name'] = ''

        # REALITY: Включаем ТОЛЬКО при наличии валидного публичного ключа (pbk)
        pbk = netquery.get('pbk')
        if pbk and isinstance(pbk, str) and pbk.strip() and pbk.strip().lower() != 'none':
            node['tls']['reality'] = {
                'enabled': True,
                'public_key': pbk.strip(),
            }
            # Обработка short_id
            sid = netquery.get('sid')
            if isinstance(sid, str) and sid.strip().lower() != "none" and sid.strip():
                node['tls']['reality']['short_id'] = sid.strip()

        # Настройка uTLS (Fingerprint)
        if netquery.get('fp'):
            node['tls']['utls'] = {
                'enabled': True,
                'fingerprint': netquery['fp']
            }
        elif node['tls'].get('reality'):
            node['tls']['utls'] = {
                'enabled': True
            }

    # Настройки транспорта (ws, grpc, http)
    if netquery.get('type'):
        if netquery['type'] == 'http':
            node['transport'] = {
                'type': 'http'
            }
        elif netquery['type'] == 'ws':
            matches = re.search(r'\?ed=(\d+)$', netquery.get('path', '/'))
            node['transport'] = {
                'type': 'ws',
                "path": netquery.get('path', '/').rsplit("?ed=", 1)[0] if matches else netquery.get('path', '/'),
                "headers": {
                    "Host": '' if netquery.get('host') is None and netquery.get('sni') == 'None' else netquery.get('host', netquery.get('sni', ''))
                }
            }
            if node.get('tls'):
                if node['tls']['server_name'] == '':
                    if node['transport']['headers']['Host']:
                        node['tls']['server_name'] = node['transport']['headers']['Host']
            if matches:
                node['transport']['early_data_header_name'] = 'Sec-WebSocket-Protocol'
                node['transport']['max_early_data'] = int(netquery.get('path', '/').rsplit("?ed=", 1)[1])
        elif netquery['type'] == 'grpc':
            node['transport'] = {
                'type': 'grpc',
                'service_name': netquery.get('serviceName', '')
            }
    elif netquery.get('obfs'):  # Поддержка параметров Shadowrocket
        if netquery['obfs'] == 'websocket':
            matches = re.search(r'\?ed=(\d+)$', netquery.get('path', '/'))
            node['transport'] = {
                'type': 'ws',
                "path": netquery.get('path', '/').rsplit("?ed=", 1)[0] if matches else netquery.get('path', '/'),
                "headers": {
                    "Host": '' if netquery.get('obfsParam') is None and netquery.get('sni') == 'None' else netquery.get('peer', netquery.get('obfsParam'))
                }
            }
            if node.get('tls'):
                if node['tls']['server_name'] == '':
                    if node['transport']['headers']['Host']:
                        node['tls']['server_name'] = node['transport']['headers']['Host']
            if matches:
                node['transport']['early_data_header_name'] = 'Sec-WebSocket-Protocol'
                node['transport']['max_early_data'] = int(netquery.get('path', '/').rsplit("?ed=", 1)[1])

    # Мультиплексирование (MUX)
    if netquery.get('protocol') in ['smux', 'yamux', 'h2mux']:
        node['multiplex'] = {
            'enabled': True,
            'protocol': netquery['protocol']
        }
        if netquery.get('max-streams'):
            node['multiplex']['max_streams'] = int(netquery['max-streams'])
        else:
            node['multiplex']['max_connections'] = int(netquery['max-connections'])
            node['multiplex']['min_streams'] = int(netquery['min-streams'])
        if netquery.get('padding') == 'True':
            node['multiplex']['padding'] = True

    return node