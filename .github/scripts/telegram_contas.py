"""
Script de notificacoes do Telegram para o Meta Viva.

Roda periodicamente via GitHub Actions (ver .github/workflows/telegram-contas.yml),
100% na nuvem - nao depende do computador de ninguem estar ligado.

O que este script faz, a cada execucao:
1. Le mensagens novas enviadas ao bot do Telegram (via getUpdates) e liga a conta
   de quem mandou "/start <codigo>" ao respectivo usuario no Firestore.
2. Para cada usuario ja ligado ao Telegram, verifica as contas cadastradas dele
   e envia um aviso quando uma conta esta a vencer em 3, 2 ou 1 dia(s), vence
   hoje, ou ja esta vencida - sem repetir o mesmo aviso duas vezes.
"""

import json
import os
from datetime import date, datetime

import firebase_admin
import requests
from firebase_admin import credentials, firestore

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API = "https://api.telegram.org/bot" + TELEGRAM_TOKEN


def inicializar_firebase():
    info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
    cred = credentials.Certificate(info)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def enviar_mensagem(chat_id, texto):
    try:
        resposta = requests.post(
            TELEGRAM_API + "/sendMessage",
            json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
            timeout=15,
        )
        if not resposta.ok:
            print("Falha ao enviar mensagem para " + str(chat_id) + ": " + resposta.text)
    except Exception as erro:
        print("Erro ao enviar mensagem para " + str(chat_id) + ": " + str(erro))


def carregar_offset(db):
    doc = db.collection("sistema").document("telegram_bot").get()
    if doc.exists:
        return doc.to_dict().get("offset", 0)
    return 0


def salvar_offset(db, novo_offset):
    db.collection("sistema").document("telegram_bot").set({"offset": novo_offset}, merge=True)


def processar_conexoes(db):
    """Le mensagens novas do bot e liga a conta de quem mandou /start <codigo>."""
    offset = carregar_offset(db)
    resposta = requests.get(
        TELEGRAM_API + "/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=20,
    )
    resposta.raise_for_status()
    dados = resposta.json()

    if not dados.get("ok"):
        print("getUpdates falhou: " + str(dados))
        return

    maior_update_id = offset - 1

    for update in dados.get("result", []):
        update_id = update.get("update_id", 0)
        if update_id > maior_update_id:
            maior_update_id = update_id

        mensagem = update.get("message") or {}
        texto = (mensagem.get("text") or "").strip()
        chat = mensagem.get("chat") or {}
        chat_id = chat.get("id")

        if chat_id is None or not texto.startswith("/start"):
            continue

        partes = texto.split(maxsplit=1)
        if len(partes) < 2:
            enviar_mensagem(
                chat_id,
                "Ola! Para ligar sua conta, toque no botao 'Acompanhar no Telegram' "
                "dentro do Meta Viva, na aba Contas.",
            )
            continue

        codigo = partes[1].strip()
        consulta = (
            db.collection("usuarios")
            .where("telegramLinkCode", "==", codigo)
            .limit(1)
            .stream()
        )
        usuario_encontrado = next(iter(consulta), None)

        if usuario_encontrado is None:
            enviar_mensagem(
                chat_id,
                "Codigo invalido ou expirado. Gere um novo codigo no Meta Viva e tente de novo.",
            )
            continue

        usuario_encontrado.reference.update(
            {
                "telegramChatId": chat_id,
                "telegramLinkCode": firestore.DELETE_FIELD,
                "telegramConectadoEm": datetime.utcnow().isoformat(),
            }
        )
        enviar_mensagem(
            chat_id,
            "Conta ligada com sucesso! A partir de agora voce recebe aqui os avisos "
            "de contas a vencer, vencendo hoje ou ja vencidas.",
        )

    if maior_update_id >= offset:
        salvar_offset(db, maior_update_id + 1)


def avisar_contas_a_vencer(db):
    hoje = date.today()

    for doc in db.collection("usuarios").stream():
        dados = doc.to_dict() or {}
        chat_id = dados.get("telegramChatId")
        if not chat_id:
            continue

        moeda = dados.get("moeda", "€")
        contas = dados.get("contas") or []
        alterou = False
        avisos = []

        for conta in contas:
            if conta.get("paga"):
                continue

            vencimento_str = conta.get("vencimento")
            if not vencimento_str:
                continue

            try:
                vencimento = datetime.strptime(vencimento_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            diff_dias = (vencimento - hoje).days
            alertas = conta.setdefault(
                "telegramAlertas",
                {"d3": False, "d2": False, "d1": False, "hoje": False, "vencida": False},
            )
            nome = conta.get("nome", "Conta")
            valor = conta.get("valor", 0)
            linha_valor = moeda + " " + ("%.2f" % valor)

            if diff_dias == 3 and not alertas.get("d3"):
                avisos.append("Em 3 dias vence: <b>" + str(nome) + "</b> - " + linha_valor)
                alertas["d3"] = True
                alterou = True
            elif diff_dias == 2 and not alertas.get("d2"):
                avisos.append("Em 2 dias vence: <b>" + str(nome) + "</b> - " + linha_valor)
                alertas["d2"] = True
                alterou = True
            elif diff_dias == 1 and not alertas.get("d1"):
                avisos.append("Amanha vence: <b>" + str(nome) + "</b> - " + linha_valor)
                alertas["d1"] = True
                alterou = True
            elif diff_dias == 0 and not alertas.get("hoje"):
                avisos.append("Vence HOJE: <b>" + str(nome) + "</b> - " + linha_valor)
                alertas["hoje"] = True
                alterou = True
            elif diff_dias < 0 and not alertas.get("vencida"):
                dias_atraso = abs(diff_dias)
                avisos.append(
                    "Conta VENCIDA ha "
                    + str(dias_atraso)
                    + " dia(s): <b>"
                    + str(nome)
                    + "</b> - "
                    + linha_valor
                )
                alertas["vencida"] = True
                alterou = True

        if avisos:
            texto = "Meta Viva - avisos de contas:\n\n" + "\n".join(avisos)
            enviar_mensagem(chat_id, texto)

        if alterou:
            doc.reference.update({"contas": contas})


def main():
    db = inicializar_firebase()
    processar_conexoes(db)
    avisar_contas_a_vencer(db)


if __name__ == "__main__":
    main()
