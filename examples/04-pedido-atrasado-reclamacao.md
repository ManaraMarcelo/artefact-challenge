# Status de pedido atrasado e reclamação

> **O que demonstra:** Consulta de pedido com verificação por e-mail, detecção de atraso (previsão já passou), acolhimento da reclamação e escalonamento com protocolo e prazo de 24h úteis.
>
> Gerado por `scripts/run_examples.py` com `gemini:gemini-3.5-flash` · data simulada: quarta-feira, 25/03/2026 14:30 (loja aberta). As consultas do agente aparecem em cada resposta.

**👤 Cliente:** Boa tarde, cadê meu pedido 8? Meu e-mail é anacarol.ferreira@coldmail.com

<details><summary>🔧 Consultas do agente (1)</summary>

- `get_order_status({"contact": "anacarol.ferreira@coldmail.com", "order_id": 8})`

  ```json
  {"verificado": true, "cliente": "Ana", "order_id": 8, "data_pedido": "2026-02-15", "status": "enviado", "itens": [{"product_id": 120, "nome": "Kala KA-C Concert Mogno", "quantidade": 1}], "total": "R$ 349,90", "forma_pagamento": "cartão de débito", "codigo_rastreio": "BRJL5544332BR", "previsao_entrega": "2026-02-28", "alerta": "Previsão de entrega (2026-02-28) já passou e o pedido ainda consta como enviado. Ofereça abrir um chamado com a equipe."}
  ```

</details>

**🎸 Lia:** Boa tarde, Ana! Tudo bem?  
  
Consultei seu pedido *#8* (Ukulele Kala KA-C Concert Mogno) e vi que ele consta como **enviado**, com o código de rastreio *BRJL5544332BR*.  
  
Porém, notei que a previsão de entrega era até **28/02/2026** e já passou.   
  
Se você quiser, posso abrir um chamado agora mesmo com a nossa equipe para verificarmos o que aconteceu com a sua entrega. Posso registrar para você?

---

**👤 Cliente:** Que absurdo, já era pra ter chegado faz tempo! Quero uma solução.

<details><summary>🔧 Consultas do agente (1)</summary>

- `escalate_to_human({"summary": "Pedido #8 com atraso na entrega. Previsão era 28/02/2026 e o status ainda consta como enviado. Cliente aguarda solução.", "contact": "anacarol.ferreira@coldmail.com", "reason": "reclamacao", "order_id": 8, "customer_name": "Ana"})`

  ```json
  {"protocolo": "EM-20260325-1C24", "prazo_retorno": "até 24 horas úteis", "mensagem": "Chamado registrado e encaminhado à equipe responsável."}
  ```

</details>

**🎸 Lia:** Sinto muito por esse transtorno, Ana. Entendo perfeitamente a sua frustração com esse atraso.   
  
Abri o chamado com a nossa equipe especializada para cobrar o rastreio e solucionar isso para você o quanto antes.  
  
* **Protocolo:** EM-20260325-1C24  
* **Prazo de retorno:** até 24 horas úteis  
  
Nossa equipe entrará em contato pelo seu e-mail (anacarol.ferreira@coldmail.com) com a atualização do seu pedido.  
  
Posso te ajudar com mais alguma coisa por enquanto?

---

<sub>Tokens: entrada 12635 · saída 1891</sub>
