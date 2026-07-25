import { getRemotionEnvironment } from "remotion";

/**
 * Remotion Studio のプレビューキャンバスは拡大率(scale)と表示位置(offset)を
 * localStorage に保存する。ctrl+ホイールや二本指スクロールで一度ずらすと
 * Studio を再起動しても元に戻らないため（キー "0" でリセットできるが分かりにくい）、
 * 「Studio で確認」を押すたびに Web アプリ側が studioCompositions.tsx へ新しい
 * トークンを埋め込み、このモジュールがそれを見てビューをリセットする。
 *
 * ユーザーコードのバンドルは @remotion/studio の previewEntry より前のエントリ
 * ポイントなので（@remotion/studio-shared の getStudioEntryPoints 参照）、
 * このモジュールは Studio の React ツリーが localStorage を読む前に評価される。
 * 通常のページ読み込みではキーを消すだけで足り、ページを開いたままホットリロード
 * だけが走った場合のみリロードが要る。
 */

const TOKEN_KEY = "kirinuki.studioViewResetToken";

/** 復元手段が無く、かつユーザー設定でもない「表示状態」だけを消す。 */
const VIEW_STATE_KEYS = [
  "remotion.previewSize", // キャンバスの拡大率 + 表示位置
  "remotion.zoom-map", // タイムラインの拡大率（コンポジション別）
];

export const resetStudioViewState = (token: string) => {
  if (typeof window === "undefined" || !getRemotionEnvironment().isStudio) {
    return;
  }

  const w = window as typeof window & { __kirinukiStudioEvaluated?: boolean };
  const isHotReload = w.__kirinukiStudioEvaluated === true;
  w.__kirinukiStudioEvaluated = true;

  try {
    if (window.localStorage.getItem(TOKEN_KEY) === token) {
      return;
    }
    window.localStorage.setItem(TOKEN_KEY, token);
    for (const key of VIEW_STATE_KEYS) {
      window.localStorage.removeItem(key);
    }
    if (isHotReload) {
      // React ツリーは既に古い値を読み終わっているのでリロードが要る。
      // トークンは保存済みなのでリロード後に再入することはない。
      window.location.reload();
    }
  } catch {
    // localStorage が使えない環境（プライベートモード等）では何もしない。
  }
};
