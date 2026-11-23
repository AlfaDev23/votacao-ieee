# Sistema de votação local

Site de votação que roda num computador e é acessado pelos celulares numa rede Wi-Fi própria, sem internet.
Só precisa do Python 3 instalado (nenhuma biblioteca extra).

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `servidor.py` | O servidor. É o único programa que roda. |
| `eleicao.json` | Título, cargos e candidatos. **Edite antes da eleição.** |
| `iniciar.bat` | Clique duas vezes para ligar o servidor no Windows. |
| `publico/votar.html` | Tela do eleitor (celular). |
| `publico/admin.html` | Painel da comissão eleitoral. |
| `votacao.db` | Criado sozinho. Guarda a senha, os códigos e a contagem. |

## Cédula (`eleicao.json`)

- Cargo com **1 candidato** → o eleitor responde **Sim / Não / Voto em branco**.
- Cargo com **2 ou mais candidatos** → o eleitor escolhe um deles ou **Voto em branco**.
- Cargo com lista vazia (`"candidatos": []`) → não aparece na cédula.

Dá para editar o arquivo enquanto o servidor está ligado, na fase de preparação. Depois clique em
“Recarregar eleicao.json” no painel. Quando a votação abre, a cédula fica congelada.

## Montando a rede

1. **Roteador**: ligue um roteador sem cabo de internet. Defina nome e senha (WPA2) para o Wi-Fi.
2. **Computador servidor**: conecte ao roteador, de preferência por cabo.
3. **IP fixo**: na página de configuração do roteador, procure “Reserva de DHCP” ou “IP estático”
   e reserve um IP para o computador (por exemplo `192.168.0.10`). Assim o endereço não muda.
4. **Firewall do Windows**: abra o PowerShell **como administrador** e rode uma vez:

   ```
   netsh advfirewall firewall add rule name="Votacao" dir=in action=allow protocol=TCP localport=8000 profile=any
   ```

   Ao final da eleição, se quiser remover a regra:

   ```
   netsh advfirewall firewall delete rule name="Votacao"
   ```

   Se o Windows perguntar se o Python pode acessar a rede, clique em **Permitir** também.
5. **Perfil da rede**: deixe a rede do roteador como “Privada” nas configurações de Wi-Fi/Ethernet do Windows.
6. **Energia**: desative a suspensão do computador e deixe na tomada.

## Usando

1. Clique duas vezes em `iniciar.bat`. Na primeira vez ele pede para **criar a senha da comissão**.
   A janela mostra os endereços, por exemplo `http://192.168.0.10:8000`.
2. No computador (ou num celular da comissão), abra `http://192.168.0.10:8000/admin` e entre com a senha.
3. Confira a cédula.
4. Gere os códigos (um por eleitor e alguns de reserva) e clique em **Imprimir códigos não usados**.
   Recorte os papéis, dobre e coloque numa urna/caixa.
5. **Faça uma votação de teste** com 2 ou 3 pessoas, encerre, confira o resultado e depois use
   **Zerar tudo**. Gere e imprima os códigos de verdade.
6. No dia: mostre para todos que o painel está zerado e clique em **Abrir votação**.
7. Cada eleitor confere o nome na lista de presença, **sorteia** um papel da caixa, entra no Wi-Fi,
   abre o endereço e vota.
8. No final, clique em **Encerrar votação**. O resultado aparece no painel. Baixe o CSV, imprima e
   guarde uma cópia do arquivo `votacao.db`.

Para trocar a senha da comissão: `python servidor.py --trocar-senha`.
Para usar outra porta: defina a variável de ambiente `PORTA` antes de iniciar.

## Como o voto fica secreto

- O banco **não guarda votos individuais**. Guarda só a contagem de cada opção. Cada voto soma 1 nela.
- O código é marcado como “usado” numa tabela separada, sem horário e sem ligação com as opções escolhidas.
- As tabelas são ordenadas pelo código, não pela ordem de chegada. O servidor também não registra
  acessos (IP/horário), então não dá para reconstruir quem votou em quê pela sequência.
- Como os papéis são sorteados, nem a comissão sabe qual código ficou com quem.
- Queimar o código e somar os votos acontece numa única transação: ou os dois acontecem, ou nenhum.

## Regras de segurança embutidas

- Resultados só aparecem **depois de encerrar**, e a votação **não pode ser reaberta**.
- Muitas tentativas de código errado a partir do mesmo aparelho bloqueiam esse aparelho por 10 minutos.
- O painel da comissão exige senha. “Zerar tudo” pede a senha de novo.
- A conferência no resultado verifica se a soma de cada cargo bate com o número de eleitores.

## Limitações conhecidas

- A conexão é HTTP, sem criptografia. Quem tem a senha do Wi-Fi e ferramentas de captura poderia,
  em tese, ver o tráfego. Mantenha a senha do Wi-Fi só com a organização e passe aos eleitores na hora.
- Quem tem acesso ao computador servidor tem acesso ao banco. Deixe o computador sob os olhos da comissão.
- Códigos não usados podem ser usados por qualquer pessoa que os tenha. Guarde as sobras com a comissão.
