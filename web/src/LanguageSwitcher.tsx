import { Button } from "antd";
import type { Language } from "./i18n";

export function LanguageSwitcher({
  language,
  onChange,
}: {
  language: Language;
  onChange: () => void;
}) {
  return (
    <Button onClick={onChange}>{language === "zh" ? "English" : "中文"}</Button>
  );
}
