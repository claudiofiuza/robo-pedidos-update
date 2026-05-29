import ctypes
import json
import os
import queue
import re
import threading
import time
import traceback
import unicodedata
import winsound
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable
from tkinter import filedialog

import customtkinter as ctk
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


try:
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config_uniao.json"
LOG_PATH = APP_DIR / "historico_robo.txt"
CLIENTES_DIR = APP_DIR / "clientes"


DEFAULT_CONFIG = {
    "site_url": "https://192.168.0.100:8443/?tenant=47330692000108",
    "timeouts": {
        "page": 20,
        "element": 20,
        "login_menu": 45,
        "menu_open": 25,
        "budget_screen": 35,
        "after_login": 8,
        "after_menu_click": 2,
        "after_customer": 2,
        "after_product_search": 3,
        "after_add_item": 2
    },
    "selectors": {
        "login_user": {"by": "id", "value": "username"},
        "login_password": {"by": "id", "value": "current-password"},
        "menu_vendas": {"by": "xpath", "value": "//span[contains(normalize-space(), 'Vendas')]"},
        "menu_orcamentos": {"by": "xpath", "value": "//*[contains(normalize-space(), 'Orçamentos de faturamento') or contains(normalize-space(), 'Orcamentos de faturamento') or contains(normalize-space(), 'OrÃ§amentos de faturamento')]"},
        "menu_pedidos_faturamento": {"by": "id", "value": "pedidofaturamentocrudcontroller"},
        "btn_incluir": {"by": "id", "value": "incluir_incluir"},
        "aba_itens": {"by": "xpath", "value": "//li[contains(normalize-space(), 'Itens')]"},
        "cliente": {"by": "id", "value": "orcamentos-faturamentos_idCliente"},
        "cliente_pedido_faturamento": {"by": "id", "value": "pedidos-faturamentos_idCliente"},
        "produto": {"by": "id", "value": "PromptProduto_produto"},
        "quantidade": {"by": "id", "value": "FaturamentoItemModel_quantidade"},
        "btn_incluir_item": {"by": "id", "value": "itens_incluirItem"}
    }
}


BY_MAP = {
    "id": By.ID,
    "xpath": By.XPATH,
    "css": By.CSS_SELECTOR,
    "name": By.NAME,
    "class": By.CLASS_NAME,
    "tag": By.TAG_NAME,
    "link": By.LINK_TEXT,
    "partial_link": By.PARTIAL_LINK_TEXT
}


@dataclass
class ItemPedido:
    codigo: str
    quantidade: str
    descricao: str = ""
    unidade: str = ""
    texto_original: str = ""
    confianca: int = 0


@dataclass
class Pedido:
    documento: str
    itens: list[ItemPedido]
    cliente: str = ""


def carregar_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        return DEFAULT_CONFIG.copy()

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    return mesclar_dict(DEFAULT_CONFIG.copy(), config)


def mesclar_dict(base: dict, extra: dict) -> dict:
    for chave, valor in extra.items():
        if isinstance(valor, dict) and isinstance(base.get(chave), dict):
            mesclar_dict(base[chave], valor)
        else:
            base[chave] = valor
    return base


def somente_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def normalizar_texto(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", valor or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.upper()
    texto = texto.replace(" P ", " PARA ").replace(" C/", " COM ")
    texto = texto.replace("TEMP ", "TEMPERO ")
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def tokens_busca(valor: str) -> set[str]:
    tokens = set()
    for token in normalizar_texto(valor).split():
        if len(token) <= 1:
            continue
        if token.endswith("S") and len(token) > 3:
            token = token[:-1]
        tokens.add(token)
    return tokens


def unidade_padrao(valor: str) -> str:
    valor = normalizar_texto(valor)
    mapa = {
        "FRD": "FD",
        "FARDO": "FD",
        "FARDOS": "FD",
        "FD": "FD",
        "CAIXA": "CX",
        "CAIXAS": "CX",
        "CX": "CX",
        "SACO": "SC",
        "SACOS": "SC",
        "SC": "SC",
        "KG": "KG",
        "KILO": "KG",
        "KILOS": "KG",
        "QUILO": "KG",
        "QUILOS": "KG",
        "UN": "UN"
    }
    return mapa.get(valor, valor)


def numero_brasileiro(valor: str) -> str:
    valor = str(valor or "").strip().replace(",", ".")
    try:
        numero = float(valor)
    except ValueError:
        return valor
    if numero.is_integer():
        return str(int(numero))
    return str(numero).rstrip("0").rstrip(".")


def linha_pedido_livre(linha: str) -> ItemPedido | None:
    match = re.match(
        r"^\s*(\d+(?:[,.]\d+)?)\s*(frd|fardo|fardos|fd|cx|caixa|caixas|sc|saco|sacos|kg|kilo|kilos|quilo|quilos|un)?\s*(?:de\s+)?(.+?)\s*$",
        linha,
        flags=re.I
    )
    if not match:
        return None

    quantidade, unidade, descricao = match.group(1), match.group(2) or "", match.group(3)
    descricao = descricao.strip()
    if not descricao or descricao.lower().startswith(("cliente", "cnpj", "cpf", "whats", "telefone")):
        return None
    return ItemPedido(
        codigo="",
        quantidade=numero_brasileiro(quantidade),
        unidade=unidade_padrao(unidade),
        descricao=descricao,
        texto_original=linha
    )


def ler_pedido(texto: str) -> Pedido:
    texto = texto or ""

    doc_match = (
        re.search(r"CNPJ/CPF:\s*([\d.\-/]+)", texto, flags=re.I)
        or re.search(r"CNPJ:\s*([\d.\-/]+)", texto, flags=re.I)
        or re.search(r"CPF:\s*([\d.\-/]+)", texto, flags=re.I)
        or re.search(r"\b(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})\b", texto)
        or re.search(r"\b(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\b", texto)
    )
    documento = somente_digitos(doc_match.group(1)) if doc_match else ""

    cliente_match = re.search(r"(?:Cliente|Nome|Raz[aã]o social):\s*(.+)", texto, flags=re.I)
    cliente = cliente_match.group(1).strip() if cliente_match else ""

    itens: list[ItemPedido] = []
    vistos: set[tuple[str, str]] = set()

    padroes = [
        r"\[[^\]]*d:\s*([A-Za-z0-9._/-]+)\]\s*(\d+(?:[,.]\d+)?)x",
        r"(?:c[oó]digo|cod|produto|sku|item)\s*[:#-]?\s*([A-Za-z0-9._/-]+).*?(?:qtd|qtde|quantidade|qde)\s*[:#-]?\s*(\d+(?:[,.]\d+)?)",
        r"(?:qtd|qtde|quantidade|qde)\s*[:#-]?\s*(\d+(?:[,.]\d+)?).*?(?:c[oó]digo|cod|produto|sku|item)\s*[:#-]?\s*([A-Za-z0-9._/-]+)",
        r"^\s*([A-Za-z0-9._/-]{2,})\s*[-x*|;, ]+\s*(\d+(?:[,.]\d+)?)\s*$"
    ]

    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha:
            continue

        for indice, padrao in enumerate(padroes):
            match = re.search(padrao, linha, flags=re.I)
            if not match:
                continue

            if indice == 2:
                quantidade, codigo = match.group(1), match.group(2)
            else:
                codigo, quantidade = match.group(1), match.group(2)

            codigo = codigo.strip().rstrip(".,;)")
            quantidade = numero_brasileiro(quantidade)
            chave = (codigo.lower(), quantidade)
            if chave not in vistos:
                vistos.add(chave)
                itens.append(ItemPedido(codigo=codigo, quantidade=quantidade, texto_original=linha, confianca=100))
            break
        else:
            item_livre = linha_pedido_livre(linha)
            if item_livre:
                chave = (normalizar_texto(item_livre.descricao), item_livre.quantidade, item_livre.unidade)
                if chave not in vistos:
                    vistos.add(chave)
                    itens.append(item_livre)

    return Pedido(documento=documento, itens=itens, cliente=cliente)


def extrair_texto_pdf(caminho_pdf: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as erro:
        raise RuntimeError("A biblioteca pypdf não está instalada. Rode: python -m pip install pypdf") from erro

    reader = PdfReader(caminho_pdf)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extrair_historico_pdf(caminho_pdf: str) -> dict:
    texto = extrair_texto_pdf(caminho_pdf)
    cliente_match = re.search(r"Cliente:\s*(.+)", texto)
    cnpj_match = re.search(r"CNPJ:\s*([\d.\-/]+)", texto)
    cliente = cliente_match.group(1).strip() if cliente_match else Path(caminho_pdf).stem
    documento = somente_digitos(cnpj_match.group(1)) if cnpj_match else normalizar_texto(cliente).replace(" ", "_")

    linhas: list[str] = []
    atual = ""
    for raw in texto.splitlines():
        linha = " ".join(raw.split())
        if not linha:
            continue
        if linha.startswith("NF "):
            if atual:
                linhas.append(atual)
            atual = linha
        elif atual and not linha.startswith(("UNIAO", "CNPJ:", "Página", "Licenciado", "Produtos comprados", "Data", "final:", "Cliente:", "Origem ")):
            atual += " " + linha
    if atual:
        linhas.append(atual)

    padrao = re.compile(
        r"^NF\s+(\S+)\s+(\d{2}/\d{2}/\d{2})\s+.*?\sdias\s+(\d+)\s+(.+?)\s+(KG|FD|CX|SC|UN)\s+(\d+(?:,\d+)?)\s+",
        flags=re.I
    )
    produtos_por_codigo: dict[str, dict] = {}
    for indice_linha, linha in enumerate(linhas):
        match = padrao.search(linha)
        if not match:
            continue

        codigo = match.group(3)
        descricao = match.group(4).strip()
        unidade = unidade_padrao(match.group(5))
        quantidade = numero_brasileiro(match.group(6))
        produto = produtos_por_codigo.setdefault(codigo, {
            "codigo": codigo,
            "descricao": descricao,
            "descricao_normalizada": normalizar_texto(descricao),
            "unidade": unidade,
            "ultima_quantidade": quantidade,
            "compras": 0,
            "ordem_recente": indice_linha
        })
        produto["compras"] += 1
        produto["ultima_quantidade"] = quantidade

    produtos = sorted(produtos_por_codigo.values(), key=lambda item: item["ordem_recente"])
    return {
        "cliente": cliente,
        "documento": documento,
        "origem_pdf": str(caminho_pdf),
        "produtos": produtos
    }


def extrair_pedido_preorcamento_pdf(caminho_pdf: str) -> Pedido:
    texto = extrair_texto_pdf(caminho_pdf)
    documentos = re.findall(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", texto)
    documento = somente_digitos(documentos[1] if len(documentos) > 1 else documentos[0]) if documentos else ""
    itens: list[ItemPedido] = []

    padrao_item = re.compile(
        r"(?m)^\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(.+?)\s*\n\s*(\d+(?:,\d+)?)\s*\n\s*(KG|UN|FD|CX|SC)\s*\n",
        flags=re.I
    )
    for match in padrao_item.finditer(texto):
        numero_item, codigo, descricao, quantidade, unidade = match.groups()
        if descricao.strip().lower() in {"código", "descricao", "descrição"}:
            continue
        itens.append(ItemPedido(
            codigo=codigo.strip(),
            quantidade=numero_brasileiro(quantidade),
            descricao=descricao.strip(),
            unidade=unidade_padrao(unidade),
            texto_original=f"Item {numero_item}: {descricao.strip()}",
            confianca=100
        ))

    return Pedido(documento=documento, itens=itens)


def pedido_para_texto(pedido: Pedido) -> str:
    linhas = []
    if pedido.documento:
        linhas.append(f"CNPJ/CPF: {pedido.documento}")
    for item in pedido.itens:
        linhas.append(f"[Cód: {item.codigo}] {item.quantidade}x - {item.descricao}")
    return "\n".join(linhas)


def salvar_historico_cliente(historico: dict) -> Path:
    CLIENTES_DIR.mkdir(exist_ok=True)
    pasta = CLIENTES_DIR / str(historico["documento"])
    pasta.mkdir(exist_ok=True)
    caminho = pasta / "historico_produtos.json"
    caminho.write_text(json.dumps(historico, indent=2, ensure_ascii=False), encoding="utf-8")
    return caminho


def encontrar_produto_historico(item: ItemPedido, historico: dict | None) -> ItemPedido:
    if item.codigo or not historico:
        return item

    consulta = normalizar_texto(item.descricao)
    consulta_tokens = tokens_busca(item.descricao)
    melhor = None
    melhor_score = 0

    for produto in historico.get("produtos", []):
        descricao = produto.get("descricao_normalizada") or normalizar_texto(produto.get("descricao", ""))
        produto_tokens = tokens_busca(produto.get("descricao", ""))
        score = int(SequenceMatcher(None, consulta, descricao).ratio() * 100)

        if consulta_tokens:
            cobertura = len(consulta_tokens & produto_tokens) / len(consulta_tokens)
            score = max(score, int(cobertura * 100))

        if item.unidade and item.unidade == produto.get("unidade"):
            score += 12

        if score > melhor_score:
            melhor_score = score
            melhor = produto

    if melhor and melhor_score >= 58:
        return ItemPedido(
            codigo=str(melhor["codigo"]),
            quantidade=item.quantidade,
            descricao=melhor["descricao"],
            unidade=melhor.get("unidade", item.unidade),
            texto_original=item.texto_original,
            confianca=min(melhor_score, 100)
        )

    item.confianca = melhor_score
    return item


class RoboUniao:
    def __init__(self, config: dict, log: Callable[[str, str], None], should_stop: Callable[[], bool], wait_pause: Callable[[], None]):
        self.config = config
        self.log = log
        self.should_stop = should_stop
        self.wait_pause = wait_pause
        self.driver = None

    def abrir_navegador(self):
        if self.driver:
            return

        self.log("Abrindo Chrome...", "info")
        chrome_options = Options()
        chrome_options.add_experimental_option("detach", True)
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_argument("--start-maximized")

        service = Service(ChromeDriverManager().install())
        service.creation_flags = 0x08000000
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.driver.set_page_load_timeout(self.config["timeouts"]["page"])

    def fechar(self):
        if not self.driver:
            return
        try:
            self.driver.quit()
        except WebDriverException:
            pass
        self.driver = None

    def locator(self, nome: str) -> tuple[str, str]:
        item = self.config["selectors"][nome]
        return BY_MAP[item["by"]], item["value"]

    def wait(self, seconds: int | None = None) -> WebDriverWait:
        return WebDriverWait(self.driver, seconds or self.config["timeouts"]["element"])

    def elemento(self, nome: str, clickavel: bool = True, timeout: int | None = None, descricao: str = ""):
        by, value = self.locator(nome)
        condicao = EC.element_to_be_clickable((by, value)) if clickavel else EC.presence_of_element_located((by, value))
        try:
            return self.wait(timeout).until(condicao)
        except TimeoutException as erro:
            alvo = descricao or nome
            raise RuntimeError(f"Não encontrei '{alvo}' a tempo. Confira se a tela terminou de carregar.") from erro

    def clicar(self, nome: str, descricao: str, timeout: int | None = None):
        self.wait_pause()
        elemento = self.elemento(nome, clickavel=True, timeout=timeout, descricao=descricao)
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elemento)
        try:
            ActionChains(self.driver).move_to_element(elemento).pause(0.2).click(elemento).perform()
        except Exception:
            try:
                elemento.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", elemento)
        self.log(descricao, "ok")

    def preencher(self, nome: str, valor: str, enter: bool = False, tab: bool = False, timeout: int | None = None):
        self.wait_pause()
        campo = self.elemento(nome, clickavel=True, timeout=timeout)
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", campo)
        campo.click()
        campo.send_keys(Keys.CONTROL, "a")
        campo.send_keys(Keys.BACKSPACE)
        campo.send_keys(str(valor))
        if enter:
            campo.send_keys(Keys.ENTER)
        if tab:
            campo.send_keys(Keys.TAB)

    def esperar_visivel(self, nome: str, descricao: str, timeout: int | None = None):
        elemento = self.elemento(nome, clickavel=False, timeout=timeout, descricao=descricao)
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elemento)
        return elemento

    def clicar_xpath_texto(self, xpath: str, descricao: str, timeout: int | None = None):
        elemento = WebDriverWait(self.driver, timeout or self.config["timeouts"]["element"]).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        elemento.click()
        self.log(descricao, "ok")

    def abrir_tela_faturamento(self, destino: str):
        vendas_xpath = "//span[contains(text(), 'Vendas')]"
        orcamentos_xpath = "//*[contains(text(), 'Orçamentos de faturamento')]"

        self.clicar_xpath_texto(vendas_xpath, "Menu Vendas aberto.", self.config["timeouts"]["login_menu"])
        time.sleep(3)

        if destino == "pedido":
            self.clicar("menu_pedidos_faturamento", "Tela de pedidos de faturamento solicitada.", self.config["timeouts"]["menu_open"])
        else:
            self.clicar_xpath_texto(orcamentos_xpath, "Tela de orçamentos solicitada.", self.config["timeouts"]["menu_open"])
        time.sleep(6)

    def logar_e_abrir_lancamento(self, usuario: str, senha: str, destino: str = "orcamento"):
        self.abrir_navegador()
        self.driver.get(self.config["site_url"])
        self.log("Site aberto.", "ok")

        self.preencher("login_user", usuario)
        self.preencher("login_password", senha, enter=True)
        self.log("Login enviado. Aguardando carregar o menu principal...", "info")
        time.sleep(self.config["timeouts"]["after_login"])

        self.abrir_tela_faturamento(destino)

        nome_tela = "pedido de faturamento" if destino == "pedido" else "orçamento"
        self.esperar_visivel("btn_incluir", f"botão Incluir do {nome_tela}", self.config["timeouts"]["budget_screen"])
        self.clicar("btn_incluir", f"Novo {nome_tela} iniciado.", self.config["timeouts"]["budget_screen"])

    def logar_e_abrir_orcamento(self, usuario: str, senha: str):
        self.logar_e_abrir_lancamento(usuario, senha, "orcamento")

    def abrir_aba_itens(self):
        self.clicar("aba_itens", "Aba Itens aberta.")

    def preencher_cliente(self, documento: str, destino: str = "orcamento") -> bool:
        if not documento:
            self.log("CPF/CNPJ não encontrado no texto.", "erro")
            return False

        seletor_cliente = "cliente_pedido_faturamento" if destino == "pedido" else "cliente"
        if seletor_cliente not in self.config.get("selectors", {}):
            seletor_cliente = "cliente"

        self.preencher(seletor_cliente, documento)
        time.sleep(self.config["timeouts"]["after_customer"])
        campo = self.elemento(seletor_cliente, clickavel=True)
        campo.send_keys(Keys.ARROW_DOWN)
        campo.send_keys(Keys.ENTER)
        self.log(f"Cliente selecionado pelo documento {documento}.", "ok")
        return True

    def incluir_item(self, item: ItemPedido, atual: int, total: int):
        if self.should_stop():
            return

        self.wait_pause()
        self.log(f"{atual}/{total} lançando produto {item.codigo}, quantidade {item.quantidade}.", "info")
        self.preencher("produto", item.codigo, enter=True)
        time.sleep(self.config["timeouts"]["after_product_search"])
        self.preencher("quantidade", item.quantidade, tab=True)
        self.clicar("btn_incluir_item", f"Produto {item.codigo} incluído.")
        time.sleep(self.config["timeouts"]["after_add_item"])

    def incluir_produtos(self, itens: Iterable[ItemPedido]):
        itens = list(itens)
        if not itens:
            self.log("Nenhum produto encontrado para lançar.", "erro")
            return

        for indice, item in enumerate(itens, 1):
            if self.should_stop():
                self.log("Processo interrompido.", "erro")
                return
            if not item.codigo:
                self.log(f"Produto sem código, revise antes de lançar: {item.texto_original or item.descricao}", "erro")
                continue
            try:
                self.incluir_item(item, indice, len(itens))
            except Exception as erro:
                self.log(f"Falha no produto {item.codigo}: {erro}", "erro")
                LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")

        self.log("Produtos finalizados.", "ok")
        for _ in range(3):
            winsound.Beep(1500, 160)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.config_data = carregar_config()
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.cancelar = False
        self.pausado = False
        self.trabalhando = False
        self.historico_cliente = None
        self.destino_var = ctk.StringVar(value="orcamento")
        self.robo = RoboUniao(self.config_data, self.log_threadsafe, self.deve_parar, self.aguardar_pausa)

        self.title("Bot de Pedidos")
        self.geometry("900x880")
        self.minsize(820, 760)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color="#0f172a")
        self.protocol("WM_DELETE_WINDOW", self.ao_fechar)

        self.montar_tela()
        self.after(150, self.processar_logs)

    def montar_tela(self):
        card_bg = "#111827"
        card_border = "#243044"
        muted = "#94a3b8"

        header = ctk.CTkFrame(self, fg_color="#172033", corner_radius=14)
        header.pack(padx=20, pady=(18, 10), fill="x")
        titulo = ctk.CTkLabel(header, text="Bot de Pedidos", font=("Roboto", 28, "bold"), text_color="#f8fafc")
        titulo.pack(anchor="w", padx=18, pady=(16, 2))
        subtitulo = ctk.CTkLabel(header, text="WhatsApp, PDF, historico do cliente e lancamento no Uniplus em um fluxo so.", text_color=muted)
        subtitulo.pack(anchor="w", padx=18, pady=(0, 16))

        frame_login = ctk.CTkFrame(self, fg_color=card_bg, corner_radius=12, border_width=1, border_color=card_border)
        frame_login.pack(padx=20, pady=8, fill="x")
        frame_login.grid_columnconfigure(1, weight=1)
        frame_login.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(frame_login, text="Acesso ao Uniplus", font=("Roboto", 15, "bold"), text_color="#e2e8f0").grid(row=0, column=0, columnspan=4, padx=14, pady=(12, 0), sticky="w")
        ctk.CTkLabel(frame_login, text="Usuario", text_color=muted).grid(row=1, column=0, padx=14, pady=10)
        self.ent_user = ctk.CTkEntry(frame_login, height=36, border_color="#334155")
        self.ent_user.grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkLabel(frame_login, text="Senha", text_color=muted).grid(row=1, column=2, padx=14, pady=10)
        self.ent_pass = ctk.CTkEntry(frame_login, width=150, show="*", height=36, border_color="#334155")
        self.ent_pass.grid(row=1, column=3, padx=10, pady=10, sticky="ew")

        frame_destino = ctk.CTkFrame(self, fg_color=card_bg, corner_radius=12, border_width=1, border_color=card_border)
        frame_destino.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(frame_destino, text="Lancar em", font=("Roboto", 15, "bold"), text_color="#e2e8f0").pack(side="left", padx=14, pady=12)
        self.segmento_destino = ctk.CTkSegmentedButton(
            frame_destino,
            values=["Orcamento de faturamento", "Pedido de faturamento"],
            command=self.alterar_destino,
            height=36,
            selected_color="#2563eb",
            selected_hover_color="#1d4ed8",
            unselected_color="#1e293b",
            unselected_hover_color="#334155"
        )
        self.segmento_destino.pack(side="left", padx=14, pady=12, fill="x", expand=True)
        self.segmento_destino.set("Orcamento de faturamento")

        frame_botoes = ctk.CTkFrame(self, fg_color=card_bg, corner_radius=12, border_width=1, border_color=card_border)
        frame_botoes.pack(padx=20, pady=8, fill="x")
        for col in range(3):
            frame_botoes.grid_columnconfigure(col, weight=1)

        ctk.CTkLabel(frame_botoes, text="Acoes rapidas", font=("Roboto", 15, "bold"), text_color="#e2e8f0").grid(row=0, column=0, columnspan=3, padx=14, pady=(12, 4), sticky="w")
        ctk.CTkButton(frame_botoes, text="Abrir e logar", height=38, command=lambda: self.iniciar_thread("login"), fg_color="#2563eb").grid(row=1, column=0, padx=(14, 6), pady=6, sticky="ew")
        ctk.CTkButton(frame_botoes, text="Preencher cliente", height=38, command=lambda: self.iniciar_thread("cliente"), fg_color="#334155").grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        ctk.CTkButton(frame_botoes, text="Abrir aba itens", height=38, command=lambda: self.iniciar_thread("aba_itens"), fg_color="#334155").grid(row=1, column=2, padx=(6, 14), pady=6, sticky="ew")
        ctk.CTkButton(frame_botoes, text="Lancar produtos", height=38, command=lambda: self.iniciar_thread("produtos"), fg_color="#3f6212").grid(row=2, column=0, padx=(14, 6), pady=(6, 14), sticky="ew")
        ctk.CTkButton(frame_botoes, text="Importar historico PDF", height=38, command=self.importar_pdf_cliente, fg_color="#6d28d9").grid(row=2, column=1, padx=6, pady=(6, 14), sticky="ew")
        ctk.CTkButton(frame_botoes, text="Importar pedido PDF", height=38, command=self.importar_pedido_pdf, fg_color="#92400e").grid(row=2, column=2, padx=(6, 14), pady=(6, 14), sticky="ew")

        frame_pedido = ctk.CTkFrame(self, fg_color=card_bg, corner_radius=12, border_width=1, border_color=card_border)
        frame_pedido.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(frame_pedido, text="Pedido recebido", font=("Roboto", 15, "bold"), text_color="#e2e8f0").pack(anchor="w", padx=14, pady=(12, 6))
        self.txt_pedido = ctk.CTkTextbox(frame_pedido, height=210, fg_color="#0b1220", border_color="#334155", border_width=1)
        self.txt_pedido.pack(padx=14, pady=(0, 12), fill="x")

        self.lbl_historico = ctk.CTkLabel(frame_pedido, text="Historico do cliente: nenhum PDF importado nesta sessao.", text_color=muted, anchor="w")
        self.lbl_historico.pack(padx=14, pady=(0, 10), fill="x")

        frame_leitura = ctk.CTkFrame(self, fg_color=card_bg, corner_radius=12, border_width=1, border_color=card_border)
        frame_leitura.pack(padx=20, pady=8, fill="x")
        frame_leitura.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(frame_leitura, text="Ler pedido", command=self.atualizar_preview, width=140, height=38, fg_color="#0f766e").grid(row=0, column=0, padx=14, pady=14)
        self.lbl_preview = ctk.CTkLabel(frame_leitura, text="Nenhum pedido lido ainda.", anchor="w", justify="left", wraplength=680, text_color="#cbd5e1")
        self.lbl_preview.grid(row=0, column=1, padx=(0, 14), pady=14, sticky="ew")

        self.btn_completo = ctk.CTkButton(
            self,
            text="INICIAR PROCESSO COMPLETO",
            command=lambda: self.iniciar_thread("completo"),
            fg_color="#16a34a",
            hover_color="#15803d",
            font=("Roboto", 17, "bold"),
            height=54,
            corner_radius=10
        )
        self.btn_completo.pack(padx=20, pady=12, fill="x")

        frame_controle = ctk.CTkFrame(self, fg_color="transparent")
        frame_controle.pack(padx=20, pady=4, fill="x")
        frame_controle.grid_columnconfigure(0, weight=1)
        frame_controle.grid_columnconfigure(1, weight=1)
        self.btn_pausar = ctk.CTkButton(frame_controle, text="Pausar", command=self.alternar_pausa, fg_color="#d97706", hover_color="#b45309", height=38)
        self.btn_pausar.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(frame_controle, text="Parar", command=self.parar, fg_color="#b91c1c", hover_color="#991b1b", height=38).grid(row=0, column=1, padx=(6, 0), sticky="ew")

        self.status_label = ctk.CTkLabel(self, text="Status: aguardando.", text_color="#fde68a", font=("Roboto", 14, "bold"))
        self.status_label.pack(pady=8)

        self.txt_log = ctk.CTkTextbox(self, height=150, fg_color="#020617", border_color=card_border, border_width=1)
        self.txt_log.pack(padx=20, pady=(4, 16), fill="both", expand=True)

    def pedido_atual(self) -> Pedido:
        pedido = ler_pedido(self.txt_pedido.get("1.0", "end"))
        pedido.itens = [encontrar_produto_historico(item, self.historico_cliente) for item in pedido.itens]
        return pedido

    def alterar_destino(self, valor: str):
        destino = "pedido" if "Pedido" in valor else "orcamento"
        self.destino_var.set(destino)
        texto = "Pedido de faturamento" if destino == "pedido" else "Orçamento de faturamento"
        self.log_threadsafe(f"Destino selecionado: {texto}.", "info")

    def atualizar_preview(self):
        pedido = self.pedido_atual()
        itens = ", ".join(
            f"{item.codigo or '?'} x {item.quantidade} ({item.confianca}%)"
            for item in pedido.itens[:8]
        )
        if len(pedido.itens) > 8:
            itens += f" e mais {len(pedido.itens) - 8}"
        self.lbl_preview.configure(
            text=f"Documento: {pedido.documento or '-'} | Cliente: {pedido.cliente or '-'} | Itens: {itens or '-'}"
        )

    def importar_pdf_cliente(self):
        caminho = filedialog.askopenfilename(
            title="Selecione o PDF de produtos comprados do cliente",
            filetypes=[("PDF", "*.pdf"), ("Todos os arquivos", "*.*")]
        )
        if not caminho:
            return

        def tarefa():
            try:
                self.log_threadsafe("Lendo PDF do cliente...", "info")
                historico = extrair_historico_pdf(caminho)
                salvar_historico_cliente(historico)
                self.historico_cliente = historico
                total = len(historico.get("produtos", []))
                texto = f"Histórico: {historico['cliente']} | {total} produtos importados."
                self.after(0, lambda: self.lbl_historico.configure(text=texto, text_color="lightgreen"))
                self.log_threadsafe(texto, "ok")
                self.after(0, self.atualizar_preview)
            except Exception as erro:
                self.log_threadsafe(f"Erro ao importar PDF: {erro}", "erro")

        threading.Thread(target=tarefa, daemon=True).start()

    def importar_pedido_pdf(self):
        caminho = filedialog.askopenfilename(
            title="Selecione o PDF do pré-orçamento/pedido",
            filetypes=[("PDF", "*.pdf"), ("Todos os arquivos", "*.*")]
        )
        if not caminho:
            return

        def tarefa():
            try:
                self.log_threadsafe("Lendo PDF do pedido...", "info")
                pedido = extrair_pedido_preorcamento_pdf(caminho)
                if not pedido.itens:
                    raise RuntimeError("não encontrei itens com código e quantidade nesse PDF.")
                texto = pedido_para_texto(pedido)
                self.after(0, lambda: self.txt_pedido.delete("1.0", "end"))
                self.after(0, lambda: self.txt_pedido.insert("1.0", texto))
                self.log_threadsafe(f"Pedido PDF importado com {len(pedido.itens)} itens.", "ok")
                self.after(0, self.atualizar_preview)
            except Exception as erro:
                self.log_threadsafe(f"Erro ao importar pedido PDF: {erro}", "erro")

        threading.Thread(target=tarefa, daemon=True).start()

    def log_threadsafe(self, mensagem: str, nivel: str = "info"):
        self.log_queue.put((mensagem, nivel))

    def processar_logs(self):
        while not self.log_queue.empty():
            mensagem, nivel = self.log_queue.get()
            hora = time.strftime("%H:%M:%S")
            linha = f"[{hora}] {mensagem}\n"
            self.txt_log.insert("end", linha)
            self.txt_log.see("end")
            cor = {"ok": "lightgreen", "erro": "tomato", "info": "yellow"}.get(nivel, "white")
            self.status_label.configure(text=f"Status: {mensagem}", text_color=cor)
            with LOG_PATH.open("a", encoding="utf-8") as file:
                file.write(linha)
        self.after(150, self.processar_logs)

    def iniciar_thread(self, acao: str):
        if self.trabalhando:
            self.log_threadsafe("Já existe uma ação em andamento.", "erro")
            return
        self.cancelar = False
        self.trabalhando = True
        threading.Thread(target=lambda: self.executar(acao), daemon=True).start()

    def executar(self, acao: str):
        try:
            usuario = self.ent_user.get().strip()
            senha = self.ent_pass.get().strip()
            pedido = self.pedido_atual()
            destino = self.destino_var.get()
            self.after(0, self.atualizar_preview)

            if acao == "login":
                self.validar_login(usuario, senha)
                self.robo.logar_e_abrir_lancamento(usuario, senha, destino)
            elif acao == "cliente":
                self.exigir_navegador()
                self.robo.preencher_cliente(pedido.documento, destino)
            elif acao == "aba_itens":
                self.exigir_navegador()
                self.robo.abrir_aba_itens()
            elif acao == "produtos":
                self.exigir_navegador()
                self.robo.incluir_produtos(pedido.itens)
            elif acao == "completo":
                self.validar_login(usuario, senha)
                self.robo.logar_e_abrir_lancamento(usuario, senha, destino)
                if self.robo.preencher_cliente(pedido.documento, destino):
                    self.robo.abrir_aba_itens()
                    self.robo.incluir_produtos(pedido.itens)
        except TimeoutException as erro:
            self.log_threadsafe(f"Campo ou botão não apareceu a tempo: {erro}", "erro")
        except Exception as erro:
            self.log_threadsafe(f"Erro: {erro}", "erro")
            with LOG_PATH.open("a", encoding="utf-8") as file:
                file.write(traceback.format_exc() + "\n")
        finally:
            self.trabalhando = False

    def validar_login(self, usuario: str, senha: str):
        if not usuario or not senha:
            raise ValueError("preencha usuário e senha.")

    def exigir_navegador(self):
        if not self.robo.driver:
            raise ValueError("abra e faça login primeiro.")

    def deve_parar(self) -> bool:
        return self.cancelar

    def aguardar_pausa(self):
        while self.pausado and not self.cancelar:
            time.sleep(0.2)

    def alternar_pausa(self):
        self.pausado = not self.pausado
        self.btn_pausar.configure(text="Retomar" if self.pausado else "Pausar")
        self.log_threadsafe("Pausado." if self.pausado else "Retomado.", "info")

    def parar(self):
        self.cancelar = True
        self.pausado = False
        self.btn_pausar.configure(text="Pausar")
        self.log_threadsafe("Parada solicitada.", "erro")

    def ao_fechar(self):
        self.cancelar = True
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
