import json, os, time, requests, importlib, argparse, yaml, ruamel.yaml
import re
import socket
import sys
import threading
from utils import tool
import warnings
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from collections import OrderedDict
from parsers.clash2base64 import clash2v2ray

ROOT = Path(__file__).resolve().parents[1]

from config.settings import get_settings
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
    template_dir = get_settings().paths.config_template_dir
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
            # Тип передаём явно: в подписях вида "[🇺🇸] [vm-no-ws] [...]"
            # от имени остаётся один флаг, и без протокола два разных
            # сервера США получили бы одинаковый тег "🇺🇸".
            node['tag'] = tool.rename(node['tag'], node.get('type'))
            if node.get('detour'):
                node['detour'] = tool.rename(node['detour'])


def nodefilter(nodes, subscribe):
    if subscribe.get('ex-node-name'):
        ex_nodename = re.split(r'[,\|]', subscribe['ex-node-name'])
        for exns in ex_nodename:
            for node in nodes[:]:  # Итерация по копии nodes для безопасного удаления элементов
                if exns in node['tag']:
                    nodes.remove(node)


# Хвостовые метки возможностей, которые export_whitelist_lines дописывает
# к строке:  vmess://<base64>[openrouter][openai][speed-slow]
# urllib такую строку не разбирает: в netloc появляются скобки, и он
# принимает их за литерал IPv6 -> ValueError("Invalid IPv6 URL").
_TAG_SUFFIX = re.compile(r"(?:\[[A-Za-z0-9_.\-]+\])+$")


def strip_capability_tags(url):
    """Отбрасывает метки, приклеенные к САМОЙ ссылке.

    Формат строки whitelist: <uri>#<флажок> <протокол> <id>-ping-N-stable-N[метка]...

    Метки нужны фильтрам шаблона и живут в ФРАГМЕНТЕ, после '#'. Их
    трогать нельзя: тег узла берётся из фрагмента, и без меток ни один
    узел не попадёт ни в одну группу (все группы выходят пустыми).

    А вот у vmess фрагмента нет - имя сервера лежит в поле ps внутри
    base64, - и метки оказываются приклеены прямо к ссылке:
        vmess://<base64>[openrouter][openai][speed-slow]
    urlparse такую строку не берёт: скобки в адресе он принимает за
    литерал IPv6 и падает с ValueError("Invalid IPv6 URL").

    Поэтому метки срезаются ТОЛЬКО до '#'. IPv6-адрес в скобках содержит
    двоеточие и под шаблон не попадает.
    """
    url = url or ""
    head, sep, tail = url.partition("#")
    head = _TAG_SUFFIX.sub("", head).strip()
    return head + sep + tail


def get_nodes(url):
    if url.startswith('sub://'):
        url = tool.b64Decode(url[6:]).decode('utf-8')
    url = strip_capability_tags(url)
    try:
        urlstr = urlparse(url)
    except ValueError:
        # Страховка для ссылок, которые не разбираются даже без меток.
        # Одна негодная ссылка не должна ронять разбор целиком: на боевой
        # базе из-за одной такой строки не собирался ни один конфиг.
        return []
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


def _unique_node_tags(nodes):
    """Присваивает узлам уникальные теги.

    Разные серверы сплошь и рядом приходят с ОДИНАКОВЫМ именем: подписка
    отдаёт несколько узлов с общим префиксом ("🏳[openproxylist.com] vmess-"),
    и имя у них совпадает до символа. sing-box такие дубли не принимает:
    "initialize outbound[N]: duplicate tag" — и конфиг не стартует вовсе.

    Дубли переименовываются в "<имя> #2", "<имя> #3". Меняем копию, а не
    исходный узел: список узлов принадлежит вызывающему коду.
    """
    seen: dict[str, int] = {}
    out = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        node = dict(node)
        base = node.get("tag") or f"node_{index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        if count > 1:
            node["tag"] = f"{base} #{count}"
        out.append(node)
    return out


def build_singbox_config_from_nodes(base_template, nodes):
    """Создаёт один sing-box конфиг из списка узлов."""
    config = deepcopy(base_template)
    outbounds = config.get("outbounds", [])

    # Дубли имён разводим ДО всего остального: иначе группа ссылается на
    # один тег несколько раз, а sing-box падает на duplicate tag.
    nodes = _unique_node_tags(nodes)

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

    # Порядок обязателен.
    #
    # 1. Сервис без группы <метка>-best не создаётся ВООБЩЕ: нет лучших
    #    серверов — нет сервиса. Уходят все три его группы и правило.
    # 2. Пустая группа резерва сервис не отменяет: он просто соберётся без
    #    4-го сервера. Убираем только ссылку на неё.
    # 3. Остатки перенаправляются в proxy.
    outbounds = _drop_servicless_groups(outbounds)
    _drop_empty_group_refs(outbounds)
    outbounds = _drop_empty_groups(outbounds)
    config["outbounds"] = outbounds
    _drop_dangling_rules(config, outbounds)
    _repoint_empty_groups(config, outbounds)
    return config


#: Суффикс группы «лучших». Её отсутствие отменяет сервис целиком.
BEST_SUFFIX = "-best"

#: Суффикс группы резерва. Пустая группа резерва сервис НЕ отменяет.
RESERVED_SUFFIX = "-reserved"


def _drop_servicless_groups(outbounds) -> list:
    """Убирает сервисы, у которых не собралась группа лучших.

    Логика одна: НЕТ серверов — НЕТ outbound. Группа "<метка>-best" — это
    те серверы, которые сервис реально проходят и отобраны по задержке.
    Если она пуста, то сервис не набрал НИ ОДНОГО сервера, и создавать
    ради него группу-пустышку с правилом маршрута нельзя: обращаться к
    такой группе не к кому.

    Удаляется ВСЯ тройка сервиса, а не только пустая группа:
        <метка>, <метка>-best, <метка>-reserved
    иначе главная группа остаётся без лучших, а ссылка на неё в route
    ведёт в пустоту.

    Пустая группа РЕЗЕРВА сервис не отменяет: сервис собран, просто в нём
    меньше четырёх серверов. Такую группу снимет обычный _drop_empty_groups,
    а ссылку на неё — _drop_empty_group_refs.

    Группы без суффикса "-best" не трогаются вовсе: общая группа proxy
    собирается по своей метке (Global) и живёт по своим правилам.
    """
    groups = [
        ob for ob in outbounds
        if isinstance(ob, dict) and ob.get("type") in ("urltest", "selector")
    ]
    tags = {str(ob.get("tag")) for ob in groups}

    # Пустые группы лучших: их сервисы не создаются.
    dead: set[str] = set()
    for ob in groups:
        tag = str(ob.get("tag"))
        if not tag.endswith(BEST_SUFFIX):
            continue
        if ob.get("outbounds"):
            continue
        base = tag[: -len(BEST_SUFFIX)]
        dead.update({base, tag, base + RESERVED_SUFFIX})

    if not dead:
        return list(outbounds)

    result = [
        ob for ob in outbounds
        if not (isinstance(ob, dict) and str(ob.get("tag")) in dead)
    ]
    # Ссылки на удалённый сервис снимаем и из уцелевших групп.
    for ob in result:
        if not isinstance(ob, dict):
            continue
        members = ob.get("outbounds")
        if not isinstance(members, list):
            continue
        cleaned = [m for m in members if not (isinstance(m, str) and m in dead)]
        if cleaned != members:
            ob["outbounds"] = cleaned
    return result


def _drop_empty_groups(outbounds) -> list:
    """Удаляет группы, оставшиеся без участников.

    sing-box НЕ принимает группу с пустым списком outbounds: он падает на
    старте с "initialize outbound[N]: missing tags" и не поднимается
    вовсе. Поэтому пустую группу из конфига приходится убирать целиком —
    вместе со ссылками на неё из других групп.

    Ссылки снимаются повторно, каскадом: удаление одной пустой группы
    может опустошить другую, которая ссылалась только на неё.
    """
    result = list(outbounds)
    for _ in range(8):
        empty = {
            str(ob.get("tag")) for ob in result
            if isinstance(ob, dict)
            and ob.get("type") in ("urltest", "selector")
            and not (ob.get("outbounds") or [])
        }
        if not empty:
            break
        result = [
            ob for ob in result
            if not (isinstance(ob, dict) and str(ob.get("tag")) in empty)
        ]
        for ob in result:
            if not isinstance(ob, dict):
                continue
            members = ob.get("outbounds")
            if not isinstance(members, list):
                continue
            cleaned = [m for m in members if not (isinstance(m, str) and m in empty)]
            if cleaned != members:
                ob["outbounds"] = cleaned
    return result


def _drop_dangling_rules(config, outbounds) -> list:
    """Убирает правила маршрута, ведущие в НЕСУЩЕСТВУЮЩУЮ группу.

    Группа не создаётся, если сервис не набрал ни одного сервера (см.
    _drop_empty_groups). Правило маршрута в такую группу остаётся ссылкой
    в никуда: sing-box либо откажется стартовать, либо отправит трафик
    сервиса в общий контур, и пользователь будет думать, что сервис
    работает. Поэтому правило УДАЛЯЕТСЯ: нет outbound — нет обращения.

    Правила на существующие outbound не трогаются вовсе, включая
    ip_is_private -> direct и whitelist-ru -> direct.

    Правило с НЕСКОЛЬКИМИ outbound (список) чистится поэлементно: из него
    выбрасываются только пропавшие имена. Если не осталось ни одного,
    правило удаляется целиком.
    """
    known = {
        str(o.get("tag")) for o in outbounds
        if isinstance(o, dict) and o.get("tag")
    }
    route = config.get("route") or {}
    rules = route.get("rules")
    if not isinstance(rules, list):
        return []

    kept: list = []
    dropped: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            kept.append(rule)
            continue
        out = rule.get("outbound")
        if isinstance(out, str):
            if out in known:
                kept.append(rule)
            else:
                dropped.append(out)
            continue
        if isinstance(out, list):
            alive = [o for o in out if isinstance(o, str) and o in known]
            if not alive:
                dropped.extend(str(o) for o in out if isinstance(o, str))
                continue
            if alive != out:
                rule = dict(rule)
                rule["outbound"] = alive
            kept.append(rule)
            continue
        kept.append(rule)

    route["rules"] = kept
    config["route"] = route
    return dropped


def _repoint_empty_groups(config, outbounds) -> list:
    """Направляет правила маршрута из пустых групп в общую.

    Сервисная группа собирается пустой, когда её метку не набрал ни один
    сервер. Правило маршрута в такой группе отдаёт трафик в никуда: группа
    есть, участников нет. Перенаправляем правило в общую группу (proxy) —
    она собирается по самой лёгкой проверке и пустой почти не бывает.

    Шаг идёт ПОСЛЕ сборки узлов: только тогда видно, какие группы вышли
    пустыми. Раньше он выполнялся на шаблоне, где пустыми были все группы
    без разбора, и не делал ничего.

    Группы не удаляем: клиент видит их в конфиге и понимает, какого
    сервиса нет в базе. Возвращает список перенаправленных правил.
    """
    groups = {
        str(o.get("tag")): o for o in outbounds
        if isinstance(o, dict) and o.get("type") in ("urltest", "selector")
    }
    empty = {tag for tag, ob in groups.items() if not (ob.get("outbounds") or [])}

    # Куда перенаправлять: ТОЛЬКО общая группа. Она собирается по самой
    # лёгкой проверке (generate_204) и пустой почти не бывает.
    #
    # Брать «любую непустую» нельзя: под неё подходила группа
    # <Сервис>-reserved, и весь трафик уходил в резервный контур одного
    # случайного сервиса. Это не только бессмысленно, но и ломает конфиг —
    # у группы резерва нет собственного участника-выхода.
    fallback = None
    for name in ("proxy", "global", "Global"):
        if name in groups and name not in empty:
            fallback = name
            break
    if fallback is None:
        return []

    repointed = []
    route = config.get("route") or {}
    for rule in route.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        out = rule.get("outbound")
        current = out if isinstance(out, str) else None
        if current is None or current not in empty:
            continue
        rule["outbound"] = fallback
        repointed.append({"from": current, "to": fallback})
    final = route.get("final")
    if isinstance(final, str) and final in empty:
        route["final"] = fallback
        repointed.append({"from": final, "to": fallback})
    return repointed


def _drop_empty_group_refs(outbounds) -> None:
    """Убирает из групп ссылки на группы, оставшиеся без участников.

    Группа <Сервис> описана как ["{all}", "<Сервис>-reserved"]. После
    фильтрации "{all}" превращается в лучшие узлы сервиса, а имя
    "-reserved" остаётся как есть — даже если сам резерв пуст. Так в
    группе оказывался единственный участник "openrouter-reserved",
    который никуда не ведёт: сервис «работал», а трафик не шёл.

    Пустую группу из конфига НЕ удаляем: клиент видит её и понимает,
    какой сервис не набрал серверов. Убираем только ссылки на неё.
    """
    closed = set()
    for _ in range(4):
        empty = {
            str(ob.get("tag")) for ob in outbounds
            if isinstance(ob, dict)
            and ob.get("type") in ("urltest", "selector")
            and not (ob.get("outbounds") or [])
        }
        if empty == closed:
            break
        closed = empty
        for ob in outbounds:
            if not isinstance(ob, dict):
                continue
            members = ob.get("outbounds")
            if not isinstance(members, list):
                continue
            cleaned = [m for m in members
                       if not (isinstance(m, str) and m in empty and m != ob.get("tag"))]
            if cleaned != members:
                ob["outbounds"] = cleaned


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: Верхний порт, который вообще можно занять.
MAX_PORT = 65535

#: Диапазон, из которого берём порты для inbound'ов sing-box.
#:
#: Почему НЕ эфемерный. Windows отдаёт эфемерные порты из 49152-65535
#: (netsh int ipv4 show dynamicport tcp). reserve_ports раньше начинал обход
#: от порта, который только что выдала ОС, то есть шёл ПРЯМО по эфемерному
#: диапазону — и ОС в тот же момент могла отдать тот же номер другому
#: процессу. На 16 параллельных батчах это давало 2-6 отказов:
#:   listen tcp 127.0.0.1:55159: bind: An attempt was made to access a socket
#:   in a way forbidden by its access permissions
#: При этом тот же порт успешно биндился из Python секундой позже — то есть
#: порт был свободен, но занят в момент гонки.
#:
#: Свой диапазон ниже эфемерного эту гонку убирает: никто, кроме нас, эти
#: номера не выдаёт. Верхняя граница 49000 выбрана ниже 49152, нижняя 40000 —
#: выше обычных сервисов, которые могут занимать 3000-9000.
PORT_RANGE_START = 40000
PORT_RANGE_END = 49000
#: Сколько неудачных проверок подряд допускаем, прежде чем признать неудачу.
#: Нужно, чтобы цикл не крутился вечно, если свободных портов не хватит.
PORT_SCAN_LIMIT = 20000


#: Порты, выданные reserve_ports и ещё не возвращённые в оборот.
#: Без этого два подряд вызова reserve_ports возвращали пересекающиеся
#: наборы (замерено: A=[52909,52910,52911], B=[52910,52911,52912]), и два
#: sing-box дрались за один и тот же порт — снаружи это выглядело как
#: «батч не стартовал» и уходило в деление пополам.
_RESERVED_PORTS: set[int] = set()
#: Порты, которые УЖЕ побывали в работе. Их не выдаём повторно, пока есть
#: свежие: при делении батча пополам родитель освобождает свои порты прямо
#: перед рекурсией, и без этого списка ребёнок получал ровно те же номера
#: (замерено: parent [58411,58412,58413] -> child [58412,58413,58414]).
#: sing-box родителя к этому моменту ещё не отпустил сокеты, поэтому ребёнок
#: не мог забиндиться и снова «не стартовал» — рекурсия вырождалась в цикл.
_USED_PORTS: set[int] = set()
_RESERVED_LOCK = threading.Lock()


def release_ports(ports: "list[int] | tuple[int, ...] | None") -> None:
    """Возвращает порты в оборот после того, как sing-box остановлен.

    Вызывать обязательно: иначе набор выданных портов растёт за прогон,
    и reserve_ports начнёт перебирать всё больше занятых кандидатов.
    """
    if not ports:
        return
    with _RESERVED_LOCK:
        for p in ports:
            _RESERVED_PORTS.discard(int(p))


def _first_candidate() -> int:
    """Первый кандидат для подбора: начало СВОЕГО диапазона.

    Раньше здесь был find_free_port() — порт, который только что выдала ОС.
    Это ставило нас в эфемерный диапазон, где с нами соревнуется система.
    """
    return PORT_RANGE_START


def reserve_ports(count: int) -> list[int]:
    """Подбирает count свободных TCP-портов на 127.0.0.1.

    Обход идёт вверх от эфемерного порта, который выдала ОС. Но эфемерный
    диапазон ограничен сверху теми же 65535, и на загруженной машине обход
    доходит до края раньше, чем наберётся нужное количество: bind() на
    порту выше границы бросает OverflowError, и подбор падал целиком.

    На краю диапазона берём у ОС новый эфемерный порт и продолжаем с него,
    вместо того чтобы падать.

    Выданные порты помечаются занятыми до release_ports(): проверка bind()
    socket закрывает, то есть сама по себе порт НЕ удерживает, и без пометки
    следующий вызов выдавал те же номера повторно.
    """
    ports: list[int] = []
    # Начинаем со СВОЕГО диапазона, а не с эфемерного порта ОС: эфемерный
    # занят гонкой с системой (см. PORT_RANGE_START).
    candidate = _first_candidate()
    misses = 0
    #: Пока False — избегаем и использованных портов тоже. Став True, когда
    #: свежих не осталось; зарезервированные не берём никогда.
    _allow_reuse = False
    #: Прошли ли свой диапазон хотя бы раз (защита от бесконечного цикла).
    _wrapped = False
    # Весь подбор идёт под ОДНИМ замком.
    #
    # Раньше проверка «порт свободен» и запись в _RESERVED_PORTS были
    # разнесены, и два потока успевали взять один и тот же номер: оба видели
    # его свободным, оба удачно биндились (проверка сразу закрывает сокет и
    # порт не удерживает) и оба его записывали. Замерено на 16 параллельных
    # батчах: 4 падали с «listen tcp 127.0.0.1:51490: bind: Only one usage of
    # each socket address», а выданные наборы пересекались:
    # [51487..51490] и [51488..51491]. Снаружи это выглядело как «батч не
    # стартовал, делю пополам».
    with _RESERVED_LOCK:
        while len(ports) < count:
            if misses > PORT_SCAN_LIMIT:
                # Свежих не осталось. Раньше здесь ОЧИЩАЛСЯ _USED_PORTS
                # целиком, и защита от повтора пропадала: два соседних набора
                # снова пересекались (замерено: [54644..54649] и [54646..54651]),
                # а sing-box, которого только что убили, ещё держал порт —
                # отсюда «socket access permissions» на bind.
                #
                # Теперь повторяем только ИСПОЛЬЗОВАННЫЕ порты, но по-прежнему
                # никогда не берём зарезервированные: занятые прямо сейчас
                # (в полёте) опасны, а отслужившие — просто старые номера.
                # bind ниже всё равно проверяет фактическую занятость.
                if not _allow_reuse:
                    _allow_reuse = True
                    misses = 0
                    candidate = find_free_port()
                    continue
                misses = 0
                candidate = find_free_port()
                continue
            if candidate > PORT_RANGE_END:
                if not _wrapped:
                    # Прошли свой диапазон — один раз пробуем эфемерный,
                    # чтобы не сдаваться на загруженной машине.
                    _wrapped = True
                    candidate = find_free_port()
                    misses += 1
                    continue
                candidate = PORT_RANGE_START
                misses += 1
                continue
            if candidate in _RESERVED_PORTS:
                misses += 1
                candidate += 1
                continue
            if not _allow_reuse and candidate in _USED_PORTS:
                misses += 1
                candidate += 1
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", candidate))
                except OSError:
                    misses += 1
                    candidate += 1
                    continue
            # Регистрируем СРАЗУ, под тем же замком: иначе следующий поток
            # увидит порт свободным и заберёт его себе.
            _RESERVED_PORTS.add(candidate)
            _USED_PORTS.add(candidate)
            ports.append(candidate)
            misses = 0
            candidate += 1
    return ports
