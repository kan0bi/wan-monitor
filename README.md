# WAN Monitor para MikroTik

Página web que mostra em tempo real o tráfego (download/upload) das WAN de um MikroTik (2 a 9 WAN),
lendo os contadores das interfaces pela API RouterOS com um utilizador **só de leitura**.
Feita para correr num contentor LXC (Debian 12) no Proxmox.

## 1. No MikroTik

Ativar a API (de preferência só para o IP do servidor Proxmox/LXC):

```
/ip service set api disabled=no address=192.168.88.50/32
```

Se ainda não tens o utilizador de leitura:

```
/user group add name=monitor policy=read,api,!write,!policy,!sensitive,!test,!local,!telnet,!ssh,!ftp,!reboot,!winbox,!web,!sniff,!password,!romon
/user add name=monitor group=monitor password=UMA_SENHA_FORTE address=192.168.88.50/32
```

> O grupo tem de ter as permissões `read` **e** `api`. O grupo predefinido `read` já inclui `api`.

Ver o nome exato das interfaces WAN:

```
/interface print
```

(Se a WAN é PPPoE usa a interface `pppoe-out1`, não a `ether` por baixo.)

**Opcional – API-SSL (porta 8729):** cria um certificado e ativa `api-ssl`, depois usa
`MT_PORT=8729` e `MT_SSL=1` no ficheiro de configuração.

## 2. No Proxmox – criar o contentor

Na shell do nó Proxmox:

```bash
pveam update
pveam available | grep debian-12        # ver o nome exato do template
pveam download local debian-12-standard_12.7-1_amd64.tar.zst

pct create 120 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname wan-monitor --memory 256 --cores 1 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 --onboot 1 --password
pct start 120
```

(Muda o ID `120`, o storage e a bridge conforme o teu setup; um IP fixo é recomendado.)

## 3. Instalar a aplicação

Copia a pasta para o contentor e corre o instalador:

```bash
# no nó Proxmox
pct push 120 wan-monitor.tar.gz /root/wan-monitor.tar.gz
pct enter 120
# dentro do contentor
cd /root && tar xzf wan-monitor.tar.gz && cd wan-monitor
bash install.sh
nano /etc/wan-monitor.env        # IP do MikroTik, user, senha, interfaces
systemctl restart wan-monitor
```

Abre `http://IP-DO-CONTENTOR:8080`.

## Configuração (`/etc/wan-monitor.env`)

| Variável | Para que serve |
|---|---|
| `MT_HOST`, `MT_PORT` | IP e porta da API do MikroTik |
| `MT_USER`, `MT_PASS` | Utilizador só de leitura |
| `MT_SSL`, `MT_SSL_VERIFY` | Usar API-SSL (8729) / validar certificado |
| `WANn_IFACE` (n = 1..9) | Nome da interface no MikroTik; WAN3 em diante só aparece se estiver definida |
| `WANn_LABEL` | Nome que aparece na página |
| `WANn_GATEWAY` | Gateway mostrado no cartão (só informativo) |
| `WANn_HOST`, `WANn_USER`, `WANn_PASS`, `WANn_PORT`, `WANn_SSL` | Ler esta WAN de **outro** router (por defeito usa `MT_*`) |
| `POLL_INTERVAL` | Segundos entre leituras (2 por defeito) |
| `HISTORY_SECONDS` | Histórico guardado em memória (3600 = 1 h) |
| `DASH_USER`, `DASH_PASS` | Senha opcional para abrir a página |
| `DEMO=1` | Tráfego fictício para testar sem router |

## Acrescentar uma WAN

No `/etc/wan-monitor.env`:

```
WAN3_IFACE=ether1
WAN3_LABEL=WAN 3
WAN3_GATEWAY=192.168.15.1
```

Se essa WAN pertence a outro MikroTik, junta também `WAN3_HOST=…`, `WAN3_USER=…` e `WAN3_PASS=…`
(e cria nesse router o mesmo utilizador só de leitura com a API ativa). Depois: `systemctl restart wan-monitor`.
Com 3 WAN, os cartões ficam lado a lado e o gráfico de comparação mostra as três.

## Problemas comuns

- **"sem ligação ao router"** → `journalctl -u wan-monitor -f`. Verifica se a API está ativa,
  se o `address=` do serviço/utilizador inclui o IP do contentor e se há firewall a bloquear a 8728.
- **"interface não encontrada"** → o nome em `WANx_IFACE` não coincide com o `/interface print`.
- **Valores a zero numa WAN em failover** → normal, a WAN de backup só tem tráfego quando é usada.

O histórico vive em memória: ao reiniciar o serviço os gráficos começam do zero.

## Ecrã inteiro / monitor de parede

A página ocupa sempre o ecrã todo, sem barras de deslocamento, e as letras e os gráficos crescem com o monitor (de 1366×768 até 4K).

- Botão **⛶ Ecrã inteiro** no topo, ou tecla **F**
- **C** mostra/oculta a comparação · **1 / 2 / 3** = 5 min / 15 min / 1 h
- O rato desaparece ao fim de 3 s parado

Opções no endereço (podem combinar-se com `&`):

| URL | Efeito |
|---|---|
| `/?tv=1` | Modo TV: esconde os botões, fica só o relógio e os dados |
| `/?range=3600` | Abre logo com 1 h (300 = 5 min, 900 = 15 min) |
| `/?cmp=0` | Sem o gráfico de comparação (os gráficos das WAN ficam maiores) |

Exemplo para um PC/Raspberry Pi ligado ao monitor, a arrancar já em ecrã inteiro:

```bash
chromium --kiosk --noerrdialogs --disable-infobars "http://IP_DO_LXC:8080/?tv=1&range=900"
```
