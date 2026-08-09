import json, os, time, requests, importlib, argparse, yaml, ruamel.yaml
import re
import socket
from utils import tool
import subprocess
import threading
import warnings
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from collections import OrderedDict
from parsers.clash2base64 import clash2v2ray

ROOT = Path(__file__).resolve().parents[1]

from config.settings import SING_BOX_PATH, URLTEST_TEMPLATE, CONFIG_TEMPLATE_DIR, SING_BOX_PORT
from script.logger_utils import get_project_logger

warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.DependencyWarning)
warnings.filterwarnings("ignore", category=Warning, module="requests")

parsers_mod = {}
providers = None
color_code = [31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96]
LOGGER = get_project_logger("main")

# single allocated inbound port for this process (to avoid changing ports between runs)
_ALLOCATED_SINGBOX_PORT: int | None = None


def get_inbound_port(template_data: dict | None = None) -> int:
    """Return a single inbound port to use for sing-box runs.

    Priority:
      1. ENV `SING_BOX_PORT`
      2. `config.settings.SING_BOX_PORT` if set (>0)
      3. `listen_port` found in provided template_data (if any)
      4. allocate a free ephemeral port and cache it for the lifetime of the process
    """
    global _ALLOCATED_SINGBOX_PORT
    if _ALLOCATED_SINGBOX_PORT:
        return _ALLOCATED_SINGBOX_PORT

    # 1. environment variable
    try:
        env_val = os.getenv("SING_BOX_PORT")
        if env_val:
            port = int(env_val)
            if port > 0:
                _ALLOCATED_SINGBOX_PORT = port
                return port
    except Exception:
        pass

    # 2. settings
    try:
        if isinstance(SING_BOX_PORT, int) and SING_BOX_PORT > 0:
            _ALLOCATED_SINGBOX_PORT = int(SING_BOX_PORT)
            return _ALLOCATED_SINGBOX_PORT
    except Exception:
        pass

    # 3. template
    try:
        if isinstance(template_data, dict):
            for inbound in template_data.get("inbounds", []):
                if inbound.get("type") == "mixed" and inbound.get("listen") == "127.0.0.1":
                    p = inbound.get("listen_port")
                    if isinstance(p, int) and p > 0:
                        _ALLOCATED_SINGBOX_PORT = p
                        return p
    except Exception:
        pass

    # 4. allocate once
    port = find_free_port()
    _ALLOCATED_SINGBOX_PORT = port
    return port


def loop_color(text):
    text = '\033[1;{color}m{text}\033[0m'.format(color=color_code[0], text=text)
    color_code.append(color_code.pop(0))
    return text


def init_parsers():
    parsers_dir = ROOT / 'parsers'
    for path, dirs, files in os.walk(str(parsers_dir)):
        for file in files:
            f = os.path.splitext(file)
            if f[1] == '.py':
                mod_name = f[0]
                parsers_mod[mod_name] = importlib.import_module('parsers.' + mod_name)


def get_template():
    template_dir = CONFIG_TEMPLATE_DIR
    template_files = os.listdir(template_dir)
    template_list = [os.path.splitext(file)[0] for file in template_files if
                     file.endswith('.json')]
    template_list.sort()
    return template_list


def load_json(path):
    return json.loads(tool.readFile(path))


def process_subscribes(subscribes):
    nodes = {}
    for subscribe in subscribes:
        if 'enabled' in subscribe and not subscribe['enabled']:
            continue
        if 'sing-box-subscribe-doraemon.vercel.app' in subscribe['url']:
            continue
        _nodes = get_nodes(subscribe['url'])
        if _nodes and len(_nodes) > 0:
            add_prefix(_nodes, subscribe)
            add_emoji(_nodes, subscribe)
            nodefilter(_nodes, subscribe)
            if subscribe.get('subgroup'):
                subscribe['tag'] = subscribe['tag'] + '-' + subscribe['subgroup'] + '-' + 'subgroup'
            if not nodes.get(subscribe['tag']):
                nodes[subscribe['tag']] = []
            nodes[subscribe['tag']] += _nodes
        else:
            #print('В этой подписке узлы не найдены, пропуск')
            print('Узлы в этой подписке не найдены, пропуск')
    tool.proDuplicateNodeName(nodes)
    return nodes


def nodes_filter(nodes, filter, group):
    for a in filter:
        if a.get('for') and group not in a['for']:
            continue
        nodes = action_keywords(nodes, a['action'], a['keywords'])
    return nodes


def action_keywords(nodes, action, keywords):
    # Фильтры будут выполняться последовательно
    # "filter":[
    #         {"action":"include","keywords":[""]},
    #         {"action":"exclude","keywords":[""]}
    #     ]
    temp_nodes = []
    flag = False
    if action == 'exclude':
        flag = True
    '''
    # Фильтрация пустых ключевых слов
    '''
    # Объединение списка шаблонов в единый шаблон через '|'
    combined_pattern = '|'.join(keywords)

    # Если объединенный шаблон пуст или содержит только пробелы, вернуть исходные узлы
    if not combined_pattern or combined_pattern.isspace():
        return nodes

    # Компиляция объединенного регулярного выражения
    compiled_pattern = re.compile(combined_pattern)

    for node in nodes:
        name = node['tag']
        # Использование регулярного выражения для проверки совпадения
        match_flag = bool(compiled_pattern.search(name))

        # Использование XOR для решения о включении узла на основе действия
        if match_flag ^ flag:
            temp_nodes.append(node)

    return temp_nodes


def add_prefix(nodes, subscribe):
    if subscribe.get('prefix'):
        for node in nodes:
            node['tag'] = subscribe['prefix'] + node['tag']
            if node.get('detour'):
                node['detour'] = subscribe['prefix'] + node['detour']


def add_emoji(nodes, subscribe):
    if subscribe.get('emoji'):
        for node in nodes:
            node['tag'] = tool.rename(node['tag'])
            if node.get('detour'):
                node['detour'] = tool.rename(node['detour'])


def nodefilter(nodes, subscribe):
    if subscribe.get('ex-node-name'):
        ex_nodename = re.split(r'[,\|]', subscribe['ex-node-name'])
        for exns in ex_nodename:
            for node in nodes[:]:  # Итерация по копии nodes для безопасного удаления элементов
                if exns in node['tag']:
                    nodes.remove(node)


def get_nodes(url):
    if url.startswith('sub://'):
        url = tool.b64Decode(url[6:]).decode('utf-8')
    urlstr = urlparse(url)
    if not urlstr.scheme:
        try:
            content = tool.b64Decode(url).decode('utf-8')
            data = parse_content(content)
            processed_list = []
            for item in data:
                if isinstance(item, tuple):
                    processed_list.extend([item[0], item[1]])  # Обработка shadowtls
                else:
                    processed_list.append(item)
            return processed_list
        except:
            content = get_content_form_file(url)
    else:
        content = get_content_from_url(url)
    # print (content)
    if type(content) == dict:
        if 'proxies' in content:
            share_links = []
            for proxy in content['proxies']:
                share_links.append(clash2v2ray(proxy))
            data = '\n'.join(share_links)
            data = parse_content(data)
            processed_list = []
            for item in data:
                if isinstance(item, tuple):
                    processed_list.extend([item[0], item[1]])  # Обработка shadowtls
                else:
                    processed_list.append(item)
            return processed_list
        elif 'outbounds' in content:
            outbounds = []
            excluded_types = {"selector", "urltest", "direct", "block", "dns"}
            filtered_outbounds = [outbound for outbound in content['outbounds'] if outbound.get("type") not in excluded_types]
            outbounds.extend(filtered_outbounds)
            return outbounds
    else:
        data = parse_content(content)
        processed_list = []
        for item in data:
            if isinstance(item, tuple):
                processed_list.extend([item[0], item[1]])  # Обработка shadowtls
            else:
                processed_list.append(item)
        return processed_list


def parse_content(content):
    # firstline = tool.firstLine(content)
    # # print(firstline)
    # if not get_parser(firstline):
    #     return None
    nodelist = []
    for t in content.splitlines():
        t = t.strip()
        if len(t) == 0:
            continue
        factory = get_parser(t)
        if not factory:
            continue
        try:
            node = factory(t)
        except Exception as e:
            node = None
            LOGGER.error("parse_content failed for line: %s", t)
            LOGGER.error("exception: %s", e)
        if node:
            nodelist.append(node)
    return nodelist


def get_parser(node):
    proto = tool.get_protocol(node)
    if providers.get('exclude_protocol'):
        eps = providers['exclude_protocol'].split(',')
        if len(eps) > 0:
            eps = [protocol.strip() for protocol in eps]
            if 'hy2' in eps:
                index = eps.index('hy2')
                eps[index] = 'hysteria2'
            if proto in eps:
                return None
    if not proto or proto not in parsers_mod.keys():
        return None
    return parsers_mod[proto].parse


def get_content_from_url(url, n=10):
    UA = ''
    # print('Загрузка ссылки подписки: \033[31m' + url + '\033[0m')
    prefixes = ["vmess://", "vless://", "ss://", "ssr://", "trojan://", "tuic://", "hysteria://", "hysteria2://",
                "hy2://", "wg://", "wireguard://", "http2://", "socks://", "socks5://"]
    if any(url.startswith(prefix) for prefix in prefixes):
        response_text = tool.noblankLine(url)
        return response_text
    for subscribe in providers["subscribes"]:
        if 'enabled' in subscribe and not subscribe['enabled']:
            continue
        if subscribe['url'] == url:
            UA = subscribe.get('User-Agent', '')
    response = tool.getResponse(url, custom_user_agent=UA)
    concount = 1
    while concount <= n and not response:
        print('Ошибка подключения, выполняется попытка ' + str(concount) + ' из ' + str(n) + '...')
        # print('Ошибка подключения, повторная попытка '+str(concount)+'/'+str(n)+'...')
        response = tool.getResponse(url)
        concount = concount + 1
        time.sleep(1)
    if not response:
        print('Ошибка получения данных, подписка пропускается')
        # print('Ошибка при получении ссылки подписки, пропуск этой ссылки')
        print('----------------------------')
        pass
    try:
        response_content = response.content
        response_text = response_content.decode('utf-8-sig')  # utf-8-sig позволяет проигнорировать BOM
        #response_encoding = response.encoding
    except:
        return ''
    if response_text.isspace():
        print('По ссылке подписки не получено никакого содержимого')
        # print('Не получено ни одного прокси из ссылки подписки')
        return None
    if not response_text:
        response = tool.getResponse(url, custom_user_agent='clashmeta')
        response_text = response.text
    if any(response_text.startswith(prefix) for prefix in prefixes):
        response_text = tool.noblankLine(response_text)
        return response_text
    elif 'proxies' in response_text:
        yaml_content = response.content.decode('utf-8')
        response_text_no_tabs = yaml_content.replace('\t', ' ') #fuckU
        yaml = ruamel.yaml.YAML()
        try:
            response_text = dict(yaml.load(response_text_no_tabs))
            return response_text
        except:
            pass
    elif 'outbounds' in response_text:
        try:
            response_text = json.loads(response.text)
            return response_text
        except:
            response_text = re.sub(r'//.*', '', response_text)
            response_text = json.loads(response_text)
            return response_text
    else:
        try:
            response_text = tool.b64Decode(response_text)
            response_text = response_text.decode(encoding="utf-8")
            # response_text = bytes.decode(response_text,encoding=response_encoding)
        except:
            pass
            # traceback.print_exc()
    return response_text


def get_content_form_file(url):
    # print('Загрузка ссылки подписки: \033[31m' + url + '\033[0m')
    # encoding = tool.get_encoding(url)
    file_extension = os.path.splitext(url)[1]  # Получение расширения файла
    if file_extension.lower() == '.yaml':
        with open(url, 'rb') as file:
            content = file.read()
        yaml_data = dict(yaml.safe_load(content))
        share_links = []
        for proxy in yaml_data['proxies']:
            share_links.append(clash2v2ray(proxy))
        node = '\n'.join(share_links)
        processed_list = tool.noblankLine(node)
        return processed_list
    else:
        data = tool.readFile(url)
        data = bytes.decode(data, encoding='utf-8')
        data = tool.noblankLine(data)
        return data


def save_config(path, nodes):
    try:
        if 'auto_backup' in providers and providers['auto_backup']:
            now = datetime.now().strftime('%Y%m%d%H%M%S')
            if os.path.exists(path):
                os.rename(path, f'{path}.{now}.bak')
        if os.path.exists(path):
            os.remove(path)
            print(f"Файл удалён и будет сохранён заново: \033[33m{path}\033[0m")
            # print(f"Конфигурационный файл сохранен в: \033[33m{path}\033[0m")
        else:
            print(f"Файл не существует, выполняется сохранение: \033[33m{path}\033[0m")
            # print(f"Файл не существует, сохранение в: \033[33m{path}\033[0m")
        tool.saveFile(path, json.dumps(nodes, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Ошибка при сохранении конфигурационного файла: {str(e)}")
        # print(f"Ошибка при сохранении конфигурационного файла: {str(e)}")
        # Если произошла ошибка сохранения, попробовать сохранить еще раз через config_file_path
        config_path = json.loads(temp_json_data).get("save_config_path", "config.json")
        CONFIG_FILE_NAME = config_path
        config_file_path = os.path.join('/tmp', CONFIG_FILE_NAME)
        try:
            if os.path.exists(config_file_path):
                os.remove(config_file_path)
                print(f"Файл удалён и будет сохранён заново: \033[33m{config_file_path}\033[0m")
                # print(f"Конфигурационный файл сохранен в: \033[33m{config_file_path}\033[0m")
            else:
                print(f"Файл не существует, выполняется сохранение: \033[33m{config_file_path}\033[0m")
                # print(f"Файл не существует, сохранение в: \033[33m{config_file_path}\033[0m")
            tool.saveFile(config_file_path, json.dumps(nodes, indent=2, ensure_ascii=False))
            # print(f"Конфигурационный файл сохранен в {config_file_path}")
            # print(f"Конфигурационный файл сохранен в {config_file_path}")
        except Exception as e:
            os.remove(config_file_path)
            print(f"Файл удалён: \033[33m{config_file_path}\033[0m")
            # print(f"Файлы удалены: \033[33m{config_file_path}\033[0m")
            print(f"Ошибка при повторном сохранении конфигурационного файла: {str(e)}")
            # print(f"Ошибка при повторном сохранении конфигурационного файла: {str(e)}")


def set_proxy_rule_dns(config):
    # dns_template = {
    #     "tag": "remote",
    #     "address": "tls://1.1.1.1",
    #     "detour": ""
    # }
    config_rules = config['route']['rules']
    outbound_dns = []
    dns_rules = config['dns']['rules']
    asod = providers["auto_set_outbounds_dns"]
    for rule in config_rules:
        if rule['outbound'] not in ['block', 'dns-out']:
            if rule['outbound'] != 'direct':
                outbounds_dns_template = \
                    list(filter(lambda server: server['tag'] == asod["proxy"], config['dns']['servers']))[0]
                dns_obj = outbounds_dns_template.copy()
                dns_obj['tag'] = rule['outbound'] + '_dns'
                dns_obj['detour'] = rule['outbound']
                if dns_obj not in outbound_dns:
                    outbound_dns.append(dns_obj)
            if rule.get('type') and rule['type'] == 'logical':
                dns_rule_obj = {
                    'type': 'logical',
                    'mode': rule['mode'],
                    'rules': [],
                    'server': rule['outbound'] + '_dns' if rule['outbound'] != 'direct' else asod["direct"]
                }
                for _rule in rule['rules']:
                    child_rule = pro_dns_from_route_rules(_rule)
                    if child_rule:
                        dns_rule_obj['rules'].append(child_rule)
                if len(dns_rule_obj['rules']) == 0:
                    dns_rule_obj = None
            else:
                dns_rule_obj = pro_dns_from_route_rules(rule)
            if dns_rule_obj:
                dns_rules.append(dns_rule_obj)
    # Очистка дублирующихся правил
    _dns_rules = []
    for dr in dns_rules:
        if dr not in _dns_rules:
            _dns_rules.append(dr)
    config['dns']['rules'] = _dns_rules
    config['dns']['servers'].extend(outbound_dns)


def pro_dns_from_route_rules(route_rule):
    dns_route_same_list = ["inbound", "ip_version", "network", "protocol", 'domain', 'domain_suffix', 'domain_keyword',
                           'domain_regex', 'geosite', "source_geoip", "source_ip_cidr", "source_port",
                           "source_port_range", "port", "port_range", "process_name", "process_path", "package_name",
                           "user", "user_id", "clash_mode", "invert"]
    dns_rule_obj = {}
    for key in route_rule:
        if key in dns_route_same_list:
            dns_rule_obj[key] = route_rule[key]
    if len(dns_rule_obj) == 0:
        return None
    if route_rule.get('outbound'):
        dns_rule_obj['server'] = route_rule['outbound'] + '_dns' if route_rule['outbound'] != 'direct' else \
            providers["auto_set_outbounds_dns"]['direct']
    return dns_rule_obj


def pro_node_template(data_nodes, config_outbound, group):
    if config_outbound.get('filter'):
        data_nodes = nodes_filter(data_nodes, config_outbound['filter'], group)
    return [node.get('tag') for node in data_nodes]


def combin_to_config(config, data):
    config_outbounds = config["outbounds"] if config.get("outbounds") else None
    i = 0
    for group in data:
        if 'subgroup' in group:
            i += 1
            for out in config_outbounds:
                if out.get("outbounds"):
                    if out['tag'] == 'Proxy':
                        out["outbounds"] = [out["outbounds"]] if isinstance(out["outbounds"], str) else out["outbounds"]
                        if '{all}' in out["outbounds"]:
                            index_of_all = out["outbounds"].index('{all}')
                            out["outbounds"][index_of_all] = (group.rsplit("-", 1)[0]).rsplit("-", 1)[-1]
                            i += 1
                        else:
                            out["outbounds"].insert(i, (group.rsplit("-", 1)[0]).rsplit("-", 1)[-1])
            new_outbound = {'tag': (group.rsplit("-", 1)[0]).rsplit("-", 1)[-1], 'type': 'selector', 'outbounds': ['{' + group + '}']}
            config_outbounds.insert(-2, new_outbound)
            if 'subgroup' not in group:
                for out in config_outbounds:
                    if out.get("outbounds"):
                        if out['tag'] == 'Proxy':
                            out["outbounds"] = [out["outbounds"]] if isinstance(out["outbounds"], str) else out["outbounds"]
                            out["outbounds"].append('{' + group + '}')
    temp_outbounds = []
    if config_outbounds:
        # Получение значения "tag" для "type": "direct"
        direct_item = next((item for item in config_outbounds if item.get('type') == 'direct'), None)
        # Предварительная обработка шаблона all
        for po in config_outbounds:
            # Обработка исходящих подключений
            if po.get("outbounds"):
                if '{all}' in po["outbounds"]:
                    o1 = []
                    for item in po["outbounds"]:
                        if item.startswith('{') and item.endswith('}'):
                            _item = item[1:-1]
                            if _item == 'all':
                                o1.append(item)
                        else:
                            o1.append(item)
                    po['outbounds'] = o1
                t_o = []
                check_dup = []
                for oo in po["outbounds"]:
                    # Избегание добавления дублирующихся узлов
                    if oo in check_dup:
                        continue
                    else:
                        check_dup.append(oo)
                    # Обработка шаблона
                    if oo.startswith('{') and oo.endswith('}'):
                        oo = oo[1:-1]
                        if data.get(oo):
                            nodes = data[oo]
                            t_o.extend(pro_node_template(nodes, po, oo))
                        else:
                            if oo == 'all':
                                for group in data:
                                    nodes = data[group]
                                    t_o.extend(pro_node_template(nodes, po, group))
                    else:
                        t_o.append(oo)
                if len(t_o) == 0:
                    t_o.append(direct_item['tag'])  # Если outbound пуст, добавляем прямое подключение (direct)
                    print('В outbound {} обнаружено 0 узлов, что приведёт к невозможности запуска sing-box; проверьте корректность шаблона config.'.format(
                        po['tag']))
                    # print('Sing-Box не может запуститься, так как не найдено прокси в outbound {}. Проверьте правильность шаблона конфигурации!!'.format(po['tag']))
                    """
                    config_path = json.loads(temp_json_data).get("save_config_path", "config.json")
                    CONFIG_FILE_NAME = config_path
                    config_file_path = os.path.join('/tmp', CONFIG_FILE_NAME)
                    if os.path.exists(config_file_path):
                        os.remove(config_file_path)
                        print(f"Файл удалён: {config_file_path}")
                        # print(f"Файлы удалены: {config_file_path}")
                    sys.exit()
                    """
                po['outbounds'] = t_o
                if po.get('filter'):
                    del po['filter']
    for group in data:
        temp_outbounds.extend(data[group])
    config['outbounds'] = config_outbounds + temp_outbounds
    # Автоматическая настройка правил маршрутизации в правила DNS для предотвращения утечки DNS
    dns_tags = [server.get('tag') for server in config['dns']['servers']]
    asod = providers.get("auto_set_outbounds_dns")
    if asod and asod.get('proxy') and asod.get('direct') and asod['proxy'] in dns_tags and asod['direct'] in dns_tags:
        set_proxy_rule_dns(config)
    # Извлечение содержимого типа wireguard
    wireguard_items = [item for item in config['outbounds'] if item.get('type') == 'wireguard']
    if wireguard_items:
        endpoints = []
        for item in wireguard_items:
            endpoints.append(item)
        new_config = OrderedDict()
        for key, value in config.items():
            new_config[key] = value
            if key == 'outbounds':  # Вставка endpoint после outbounds
                new_config['endpoints'] = endpoints
        config = new_config
        # Обновление outbounds, удаление типа wireguard
        config['outbounds'] = [item for item in config['outbounds'] if item.get('type') != 'wireguard']
    return config


def updateLocalConfig(local_host, path):
    header = {
        'Content-Type': 'application/json'
    }
    r = requests.put(local_host + '/configs?force=false', json={"path": path}, headers=header)
    print(r.text)


def display_template(tl):
    print_str = ''
    for i in range(len(tl)):
        print_str += loop_color('{index}、{name} '.format(index=i + 1, name=tl[i]))
    print(print_str)


def select_config_template(tl, selected_template_index=None):
    if args.template_index is not None:
        uip = args.template_index
    else:
        # print ('Введите номер для выбора соответствующего шаблона конфигурации (нажмите Enter для выбора первого по умолчанию): ')
        uip = input('Введите номер, чтобы загрузить соответствующий шаблон config (нажмите Enter, чтобы выбрать первый шаблон по умолчанию): ')
        try:
            if uip == '':
                return 0
            uip = int(uip)
            if uip < 1 or uip > len(tl):
                print('Введена неверная информация! Введите ещё раз')
                # print('Введена неверная информация! Введите повторно')
                return select_config_template(tl)
            else:
                uip -= 1
        except:
            print('Введена неверная информация! Введите ещё раз')
            # print('Введена неверная информация! Введите повторно')
            return select_config_template(tl)
    return uip


# Пользовательская функция для парсинга аргумента в формат JSON
def parse_json(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError(f"Invalid JSON: {value}")


def load_template(template_path):
    """Загружает JSON-шаблон конфигурации sing-box."""
    return load_json(str(template_path))


def build_singbox_config_from_nodes(base_template, nodes):
    """Создаёт один sing-box конфиг из списка узлов."""
    config = deepcopy(base_template)
    outbounds = config.get("outbounds", [])

    tags = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        tag = node.get("tag", f"node_{index}")
        if tag not in tags:
            tags.append(tag)

    for outbound in outbounds:
        if isinstance(outbound.get("outbounds"), list):
            if "{all}" in outbound["outbounds"]:
                outbound["outbounds"] = tags
                break
            if any(isinstance(item, str) and item.startswith("{") and item.endswith("}") for item in outbound["outbounds"]):
                outbound["outbounds"] = tags
                break

    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        tag = node.get("tag", f"node_{index}")
        if not any(outbound.get("tag") == tag for outbound in outbounds):
            outbounds.append(node)

    config["outbounds"] = outbounds
    return config


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_singbox_admin(
    config_path,
    singbox_path=None,
    *,
    expected_debug_count=0,
    timeout=60.0,
    debug_mode=True,
):
    """Запускает sing-box и завершает его после нужного количества debug-логов или по таймауту."""
    executable = singbox_path or SING_BOX_PATH
    if not os.path.exists(executable):
        raise FileNotFoundError(f"Не найден sing-box по пути: {executable}")

    command_line = [executable, "run", "-c", config_path]
    if debug_mode:
        LOGGER.debug("Running sing-box command: %s", command_line)
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        process = subprocess.Popen(command_line, **kwargs)
    except OSError as exc:
        return [f"failed to start sing-box: {exc}"]

    output_lines = []
    debug_count = 0

    def _reader():
        nonlocal debug_count
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line:
                    continue
                text = line.rstrip("\n")
                output_lines.append(text)
                if "DEBUG[" in text:
                    debug_count += 1
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if expected_debug_count > 0 and debug_count >= expected_debug_count:
            break
        if process.poll() is not None:
            break
        time.sleep(0.1)

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    reader.join(timeout=1)
    return_code = process.returncode
    if expected_debug_count > 0 and debug_count == 0:
        LOGGER.error(
            "sing-box run returned no DEBUG urltest lines. return_code=%s output_lines=%d timeout=%s expected_debug_count=%d",
            return_code,
            len(output_lines),
            timeout,
            expected_debug_count,
        )
        for line in output_lines:
            LOGGER.error("sing-box output: %s", line)
    if return_code != 0 and len(output_lines) == 0:
        LOGGER.error("sing-box process exited with return code %s and no output", return_code)
    return [line for line in output_lines if line]


def generate_debug_configs_with_singbox(
    *,
    threads=1,
    urltest="",
    ping_limit=0,
    template=URLTEST_TEMPLATE,
    output_dir="source/tests",
    singbox_path=None,
    merge_lines=None,
):
    global providers
    """Собирает один merged-config из нескольких строк и запускает sing-box один раз."""
    if threads <= 0:
        raise ValueError("threads must be greater than zero")
    if not urltest:
        raise ValueError("urltest must not be empty")
    if ping_limit < 0:
        raise ValueError("ping_limit must not be negative")

    template_path = ROOT / template
    output_dir_path = ROOT / output_dir
    output_dir_path.mkdir(parents=True, exist_ok=True)

    old_cwd = os.getcwd()
    previous_providers = providers
    try:
        os.chdir(ROOT)
        init_parsers()
        providers = {"exclude_protocol": "", "subscribes": []}

        LOGGER.info("Loading sing-box template from %s", template_path)
        template_data = load_template(template_path)

        if merge_lines is None:
            return {
                "config_path": "",
                "command": [],
                "output": [],
                "node_count": 0,
            }

        lines = [line.strip() for line in merge_lines if line.strip()]
        LOGGER.info("Received %d merge lines", len(lines))

        parsed_nodes = []
        parsed_node_lines = []
        for line_index, line in enumerate(lines, start=1):
            nodes = get_nodes(line)
            if not nodes:
                continue
            node_index = 0
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                original_tag = node.get("tag")
                if original_tag:
                    node_index += 1
                    numbered_tag = f"{original_tag}#{line_index}"
                    if node_index > 1:
                        numbered_tag += f".{node_index}"
                    node["tag"] = numbered_tag
                parsed_nodes.append(node)
                parsed_node_lines.append(line)

        if not parsed_nodes:
            return {
                "config_path": "",
                "command": [],
                "output": [],
                "node_count": 0,
                "parsed_nodes": [],
                "parsed_node_lines": [],
            }

        config = build_singbox_config_from_nodes(template_data, parsed_nodes)
        # choose a single inbound port for the whole process (respect env/settings/template)
        inbound_port = 7891
        for inbound in config.get("inbounds", []):
            if inbound.get("type") == "mixed" and inbound.get("listen") == "127.0.0.1":
                inbound["listen_port"] = inbound_port
                break
        filename = "merged_config.json"
        target_path = output_dir_path / filename
        with target_path.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)

        LOGGER.info("Wrote merged config to %s", target_path)
        LOGGER.info("Starting sing-box with %d nodes", len(parsed_nodes))
        output_lines = run_singbox_admin(
            str(target_path),
            singbox_path=singbox_path,
            expected_debug_count=len(parsed_nodes),
            timeout=15.0,
            debug_mode=bool(os.getenv("SINGBOX_DEBUG", "") or os.getenv("DEBUG", "")),
        )
        LOGGER.info("sing-box run completed, captured %d output lines", len(output_lines))
        return {
            "config_path": str(target_path),
            "command": ["sing-box", "run", "-c", str(target_path)],
            "output": output_lines,
            "node_count": len(parsed_nodes),
            "parsed_nodes": parsed_nodes,
            "parsed_node_lines": parsed_node_lines,
        }
    finally:
        os.chdir(old_cwd)
        providers = previous_providers

