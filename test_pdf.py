import pdfplumber
from pathlib import Path

def test_extraction(pdf_path: str):
    path = Path(pdf_path)
    if not path.exists():
        print(f"❌ Файл не найден: {path}")
        return

    print(f"🔍 Анализируем PDF: {path.name}\n")

    with pdfplumber.open(path) as pdf:
        print(f"Всего страниц: {len(pdf.pages)}\n")
        
        for i, page in enumerate(pdf.pages):
            print(f"{'='*60}")
            print(f"📄 СТРАНИЦА {i + 1}")
            print(f"{'='*60}")
            
            # 1. Извлекаем обычный текст
            text = page.extract_text()
            if text:
                print("--- ТЕКСТ ---")
                # Показываем первые 500 символов, чтобы не засорять консоль
                print(text[:500] + ("..." if len(text) > 500 else ""))
            else:
                print("--- ТЕКСТ НЕ НАЙДЕН ---")
            
            print("\n")
            
            # 2. Пробуем извлечь таблицы (вдруг там структурированные данные)
            tables = page.extract_tables()
            if tables:
                print(f"--- НАЙДЕНО ТАБЛИЦ {len(tables)} ---")
                for t_idx, table in enumerate(tables):
                    print(f"Таблица {t_idx + 1} (первая строка): {table[0]}")
            else:
                print("--- ТАБЛИЦ НЕ НАЙДЕНО ---")
            
            print("\n")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Использование: python test_pdf.py <путь_к_pdf>")
    else:
        test_extraction(sys.argv[1])