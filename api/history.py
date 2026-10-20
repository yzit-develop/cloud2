"""会話履歴を、送れる量に収める（コマ7）

LLM の API は会話を覚えていない（ステートレス）。2回目以降の質問では、
それまでの会話をすべて送り直す。会話が長くなるほど、送る量（入力トークン）が増える。
そこで、送る量に上限（予算）を決め、古いメッセージから捨てる。
"""


def estimate_tokens(message):
    """メッセージ1件のトークン数を大まかに見積もる（教員が用意したもの）

    実際のトークン数は、モデルごとのトークナイザーで決まる。ここでは安全側に、
    「1文字＝1トークン」として数え、メッセージごとの付加分として4を足す。
    """
    return len(message["content"]) + 4


def trim_history(messages, budget):
    """messages（古い順）から、新しいほうを優先して、合計が budget 以内になるだけ残す

    ルール
      1. 最新のメッセージ（最後の1件）は、予算を超えていても必ず残す
      2. 新しいほうから順に見て、予算に収まる間は残す
      3. 1件でも収まらなかったら、それより古いメッセージはすべて捨てる（途中を抜かさない）
      4. 返すリストは古い順のまま。引数の messages は書き換えない
    """
    result = []
    total = 0

    for message in reversed(messages):
        cost = estimate_tokens(message)
        if result and total + cost > budget:
            break
        result.append(message)
        total += cost
    result.reverse()

    return result
