import json, os, time, requests, importlib, argparse, yaml, ruamel.yaml
import re
import socket
import sys
from utils import tool
import warnings
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from collections import OrderedDict
from parsers.clash2base64 import clash2v2ray

ROOT = Path(__file__).resolve().parents[1]

from config.settings import setting
from script.logger_utils import get_project_logger

warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.DependencyWarning)
warnings.filterwarnings("ignore", category=Warning, module="requests")

parsers_mod = {}
providers = None
color_code = [31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96]
LOGGER = get_project_logger("main")


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
    template_dir = setting("CONFIG_TEMPLATE_DIR")
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


# Сколько одинаковых пропусков на протокол писать в debug.log за процесс.
# Битая подписка легко даёт тысячи неразбираемых ссылок; без ограничения они
# забивают debug.log одинаковыми сообщениями.
_SKIP_LOG_LIMIT = 20
_skip_log_counters: dict[str, int] = {}


def _log_skipped_line(reason: str, proto: str | None, line: str) -> None:
    """Пишет пропущенную строку в debug.log, но не более _SKIP_LOG_LIMIT раз
    на протокол (дальше — только одна строка-уведомление)."""
    key = proto or "unknown"
    count = _skip_log_counters.get(key, 0) + 1
    _skip_log_counters[key] = count
    if count <= _SKIP_LOG_LIMIT:
        LOGGER.debug("parse_content: %s (строка пропущена): %s", reason, line[:200])
    elif count == _SKIP_LOG_LIMIT + 1:
        LOGGER.debug(
            "parse_content: протокол '%s': дальнейшие пропуски не логируются (счётчик)", key
        )


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
            proto = tool.get_protocol(t)
            if proto:
                _log_skipped_line(f"нет парсера для протокола '{proto}'", proto, t)
            else:
                _log_skipped_line("не удалось определить протокол", None, t)
            continue
        try:
            node = factory(t)
        except Exception as e:
            node = None
            LOGGER.warning("parse_content: парсер '%s' не смог разобрать строку: %s", tool.get_protocol(t), t[:200])
            LOGGER.debug("parse_content: исключение при разборе: %s", e)
        if node:
            nodelist.append(node)
        else:
            proto = tool.get_protocol(t)
            _log_skipped_line(f"парсер '{proto}' вернул None", proto, t)
    return nodelist


def get_parser(node):
    """Возвращает функцию разбора протокола или None.

    providers может быть None: раньше это было причиной обязательного
    «подкладывания» core.providers перед каждым разбором, а вместе с ним —
    os.chdir() в шести местах. Сейчас отсутствие настроек просто значит
    «исключений нет».
    """
    proto = tool.get_protocol(node)
    excluded = (providers or {}).get('exclude_protocol') or ''
    if excluded:
        eps = excluded.split(',')
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


# Схемы, для которых get_nodes разбирает строку ЛОКАЛЬНО (без сетевых
# запросов) — совпадает со списком prefixes в get_content_from_url.
_LOCAL_URI_PREFIXES = (
    "vmess://", "vless://", "ss://", "ssr://", "trojan://", "tuic://",
    "hysteria://", "hysteria2://", "hy2://", "wg://", "wireguard://",
    "http2://", "socks://", "socks5://",
)


def split_parsable_lines(lines):
    """Делит строки на разбираемые и нет: (parsable, unparsable).

    «Разбираемая» — строка, из которой получается хотя бы один outbound-dict,
    то есть её реально можно проверить через sing-box. Всё остальное
    (неизвестный протокол, отсутствие парсера, битый формат) проверить нельзя:
    такие строки не должны попадать в пул проверки.

    Используется на этапе сборки merge.txt, чтобы сразу отправить мусор в чс,
    а не тратить на него батчи проверки в каждом цикле.
    """
    global providers
    previous_providers = providers
    old_cwd = os.getcwd()
    parsable: list[str] = []
    unparsable: list[str] = []
    try:
        os.chdir(ROOT)
        init_parsers()
        providers = {"exclude_protocol": "", "subscribes": []}
        for raw in lines:
            text = str(raw).strip()
            if not text:
                continue
            # Только схемы, которые разбираются локально: иначе get_nodes
            # попытается скачать строку как URL подписки.
            if not text.startswith(_LOCAL_URI_PREFIXES):
                unparsable.append(text)
                continue
            try:
                nodes = get_nodes(text)
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("split_parsable_lines: ошибка разбора: %s", exc)
                nodes = None
            if nodes and any(isinstance(node, dict) for node in nodes):
                parsable.append(text)
            else:
                unparsable.append(text)
        return parsable, unparsable
    finally:
        os.chdir(old_cwd)
        providers = previous_providers


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


# Пользовательская функция для парсинга аргумента в формат JSON
def parse_json(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError(f"Invalid JSON: {value}")


def load_template(template_path):
    """Загружает JSON-шаблон конфигурации sing-box."""
    return load_json(str(template_path))


def validate_config_groups(config, *, strict: bool = True) -> list:
    """Проверяет, что в конфиге нет группы без участников, в которую ведёт маршрут.

    Пустая группа не роняет sing-box при старте: он запускается и молча не
    выпускает трафик. Раньше это доходило до репозитория — проверка в
    deploy_config смотрела только «есть ли хоть один настоящий outbound»,
    и этого хватало. Проверено на реальных данных: группа proxy с
    include ["Global"] оставалась пустой, потому что ни один сервер не
    достигает всех целей, а route.final указывал именно на неё.

    Возвращает список описаний пустых групп. При strict=True бросает
    ConfigError вместо возврата.
    """
    groups = {}
    for ob in config.get("outbounds", []) or []:
        if not isinstance(ob, dict):
            continue
        if ob.get("type") in ("urltest", "selector"):
            groups[ob.get("tag")] = len(ob.get("outbounds") or [])

    empty = {tag for tag, n in groups.items() if n == 0}
    if not empty:
        return []

    route = config.get("route") or {}
    referenced = set()
    final = route.get("final")
    if isinstance(final, str):
        referenced.add(final)
    for rule in route.get("rules", []) or []:
        if not isinstance(rule, dict):
            continue
        out = rule.get("outbound")
        if isinstance(out, str):
            referenced.add(out)
        elif isinstance(out, list):
            referenced.update(o for o in out if isinstance(o, str))

    broken = sorted(referenced & empty)
    if not broken:
        return sorted(empty)

    detail = (
        f"группы без участников, на которые ссылается маршрут: {broken}. "
        f"Всего пустых групп: {sorted(empty)}. "
        "Чаще всего причина — фильтр, который отсёк всё (например "
        "include по тегу capabilities, которого нет ни у одного сервера)."
    )
    if strict:
        raise ValueError("конфиг собрать нельзя: " + detail)
    print("ВНИМАНИЕ sing-box: " + detail, file=sys.stderr)
    return sorted(empty)


def build_singbox_config_from_nodes(base_template, nodes):
    """Создаёт один sing-box конфиг из списка узлов."""
    config = deepcopy(base_template)
    outbounds = config.get("outbounds", [])

    # Собираем все теги узлов
    all_tags = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        tag = node.get("tag", f"node_{index}")
        if tag not in all_tags:
            all_tags.append(tag)

    # Обрабатываем каждый outbound из шаблона: применяем фильтры, если они есть
    for outbound in outbounds:
        if not isinstance(outbound.get("outbounds"), list):
            continue

        # Если в outbound есть filter — фильтруем узлы перед вставкой
        if outbound.get("filter"):
            # nodes_filter ожидает список узлов (dict), а не теги.
            # Преобразуем all_tags обратно в узлы для фильтрации.
            node_map = {n.get("tag"): n for n in nodes if isinstance(n, dict) and n.get("tag")}
            filtered_nodes = nodes_filter(list(node_map.values()), outbound["filter"], "")
            filtered_tags = [n.get("tag") for n in filtered_nodes if n.get("tag")]
            # Заменяем шаблоны на отфильтрованные теги
            new_outbounds = []
            for item in outbound["outbounds"]:
                if item == "{all}" or (isinstance(item, str) and item.startswith("{") and item.endswith("}")):
                    new_outbounds.extend(filtered_tags)
                else:
                    new_outbounds.append(item)
            outbound["outbounds"] = new_outbounds
            # Удаляем filter, чтобы не уйти в итоговый JSON
            del outbound["filter"]
        else:
            # Старая логика: просто заменяем {all} и {...} на все теги
            if "{all}" in outbound["outbounds"]:
                outbound["outbounds"] = all_tags
                continue
            if any(isinstance(item, str) and item.startswith("{") and item.endswith("}") for item in outbound["outbounds"]):
                outbound["outbounds"] = all_tags
                continue

    # Добавляем сами узлы в outbounds, если их там ещё нет
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        tag = node.get("tag", f"node_{index}")
        if not any(ob.get("tag") == tag for ob in outbounds):
            outbounds.append(node)

    config["outbounds"] = outbounds
    return config


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


