export interface ClipTheme {
  label: string;
  titleBackground: string;
  titleTextColor: string;
  titleAccentColor: string;
  captionTextColor: string;
  captionActiveColor: string;
  captionActiveGlow: string;
}

export const THEMES: Record<string, ClipTheme> = {
  yumeno: {
    label: "yumeno",
    titleBackground: "linear-gradient(to right, #FF1493 0%, #B200FF 100%)",
    titleTextColor: "#FFFFFF",
    titleAccentColor: "#4A0E4E",
    captionTextColor: "#FFFFFF",
    captionActiveColor: "#fe27c8",
    captionActiveGlow: "rgba(254, 39, 200, 0.5)",
  },
  heisei_girl: {
    label: "平成女児",
    titleBackground: "linear-gradient(135deg, #FF69B4 0%, #FFB6E6 40%, #B8F0FF 65%, #40E0D0 100%)",
    titleTextColor: "#FFFFFF",
    titleAccentColor: "#880055",
    captionTextColor: "#FFFFFF",
    captionActiveColor: "#FF5184",
    captionActiveGlow: "rgba(255, 81, 132, 0.55)",
  },
};

export const DEFAULT_THEME_KEY = "yumeno";
