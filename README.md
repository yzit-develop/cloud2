# クラウド利用Ⅱ 演習リポジトリ

このリポジトリは、クラウド利用Ⅱの演習で使う自分用のリポジトリである。

## 使い方

- 開発は **GitHub Codespaces** で行う（**Code** ボタン → **Codespaces** タブ）。
- 各コマの演習は、そのコマの開始状態から始める。手順は各コマの演習手順書を見ること。

```bash
git fetch upstream
git switch -c lessonNN upstream/lessonNN-start   # NN はコマ番号（例: 08）
```

## 守ること

- **APIキーなどの秘密情報をコミットしない。** 秘密情報は `.env`（コミットされない）か、Codespaces のシークレットに置く。
- `git add` の前に、必ず `git status` で何がコミットされるかを確認する。
- 使い終わったら Codespace を**停止**する。

## メモ

（ここに自分用のメモを書いてよい）
