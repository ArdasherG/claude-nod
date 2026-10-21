#!/usr/bin/env python3
"""Режет лист со стикерами на отдельные файлы 512×512 для Telegram.

Вспомогательный инструмент, не часть хуков. Зависимостей нет: PNG читается
и пишется средствами стандартной библиотеки (zlib + разбор чанков).

    python make_stickers.py stiker_pack/лист.png
    python make_stickers.py stiker_pack/лист.png --out stiker_pack/ready

На входе ждёт PNG с прозрачным фоном, на котором стикеры разложены сеткой
и не соприкасаются. Каждый найденный стикер обрезается по содержимому,
вписывается в квадрат 512×512 с прозрачными полями и сохраняется отдельно.
"""
import os
import struct
import sys
import zlib

ALPHA_FLOOR = 16        # всё, что прозрачнее, считаем фоном
GAP = 4                 # сколько пустых пикселей разделяют стикеры
MIN_SIDE = 40           # мелочь вроде пыли пропускаем
CANVAS = 512
PAD = 0.04              # поля вокруг стикера, доля от стороны


# --------------------------------------------------------------------------- #
# Чтение и запись PNG
# --------------------------------------------------------------------------- #

def read_png(path):
    """Возвращает (ширина, высота, строки RGBA в виде bytearray)."""
    raw = open(path, "rb").read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("это не PNG")
    pos, idat, meta = 8, [], None
    while pos < len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            meta = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    width, height, depth, color, _comp, _filt, interlace = meta
    if depth != 8 or color not in (2, 6) or interlace:
        raise ValueError("поддерживаются только 8-битные RGB/RGBA без чересстрочности "
                         "(получено: глубина %d, тип %d, чересстрочность %d)"
                         % (depth, color, interlace))
    src_bpp = 4 if color == 6 else 3
    data = zlib.decompress(b"".join(idat))

    stride = width * src_bpp
    rows, prev = [], bytearray(stride)
    pos = 0
    for _ in range(height):
        filt = data[pos]
        line = bytearray(data[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        unfilter(filt, line, prev, src_bpp)
        rows.append(line)
        prev = line

    if src_bpp == 3:                      # дорисовываем непрозрачную альфу
        rows = [bytearray(b"".join(bytes(line[i:i + 3]) + b"\xff"
                                   for i in range(0, len(line), 3)))
                for line in rows]
    return width, height, rows


def unfilter(filt, line, prev, bpp):
    """Снимает построчный фильтр PNG прямо в буфере."""
    if filt == 0:
        return
    for i in range(len(line)):
        left = line[i - bpp] if i >= bpp else 0
        up = prev[i]
        if filt == 1:
            line[i] = (line[i] + left) & 0xFF
        elif filt == 2:
            line[i] = (line[i] + up) & 0xFF
        elif filt == 3:
            line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
        elif filt == 4:
            upleft = prev[i - bpp] if i >= bpp else 0
            p = left + up - upleft
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
            best = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
            line[i] = (line[i] + best) & 0xFF
        else:
            raise ValueError("неизвестный фильтр PNG: %d" % filt)


def write_png(path, width, height, rows):
    """Пишет RGBA-картинку без фильтрации строк."""
    body = bytearray()
    for line in rows:
        body.append(0)
        body.extend(line)

    def chunk(kind, payload):
        out = struct.pack(">I", len(payload)) + kind + payload
        return out + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(body), 9)))
        fh.write(chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# Поиск стикеров на листе
# --------------------------------------------------------------------------- #

def spans(flags, gap):
    """Непрерывные отрезки True, склеенные через пропуски короче gap."""
    out, start, empty = [], None, 0
    for index, value in enumerate(flags):
        if value:
            if start is None:
                start = index
            empty = 0
        elif start is not None:
            empty += 1
            if empty >= gap:
                out.append((start, index - empty + 1))
                start = None
    if start is not None:
        out.append((start, len(flags)))
    return out


def components(width, height, rows, step=2):
    """Связные области непрозрачных пикселей: (площадь, left, top, right, bottom).

    Считаем по прореженной сетке (каждый второй пиксель) — так быстрее, а на
    точность границ это почти не влияет: потом всё равно ужимаем по содержимому.
    """
    from collections import deque

    cols = (width + step - 1) // step
    lines = (height + step - 1) // step
    mask = bytearray(cols * lines)
    for y in range(lines):
        row = rows[min(y * step, height - 1)]
        base = y * cols
        for x in range(cols):
            if row[min(x * step, width - 1) * 4 + 3] > ALPHA_FLOOR:
                mask[base + x] = 1

    found = []
    for start in range(cols * lines):
        if not mask[start]:
            continue
        queue = deque([start])
        mask[start] = 0
        area = 0
        x0 = x1 = start % cols
        y0 = y1 = start // cols
        while queue:
            node = queue.popleft()
            cx, cy = node % cols, node // cols
            area += 1
            x0, x1 = min(x0, cx), max(x1, cx)
            y0, y1 = min(y0, cy), max(y1, cy)
            for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if 0 <= nx < cols and 0 <= ny < lines:
                    node2 = ny * cols + nx
                    if mask[node2]:
                        mask[node2] = 0
                        queue.append(node2)
        found.append((area, x0 * step, y0 * step,
                      min(width, (x1 + 1) * step), min(height, (y1 + 1) * step)))
    return found


def attach_small(big, small, reach=50):
    """Прилепляет мелочь (блёстки, значки) к ближайшему стикеру."""
    boxes = [list(b) for b in big]
    for _area, left, top, right, bottom in small:
        best, best_distance = None, reach
        for box in boxes:
            dx = max(box[0] - right, left - box[2], 0)
            dy = max(box[1] - bottom, top - box[3], 0)
            distance = (dx * dx + dy * dy) ** 0.5
            if distance < best_distance:
                best, best_distance = box, distance
        if best is not None:
            best[0] = min(best[0], left)
            best[1] = min(best[1], top)
            best[2] = max(best[2], right)
            best[3] = max(best[3], bottom)
    return [tuple(b) for b in boxes]


def dense(counts, extent, ratio):
    """Полоса считается содержимым, если непрозрачных пикселей больше порога.

    Полностью пустых полос между стикерами часто не бывает: мешают блёстки
    и мягкие тени. Поэтому разделителем считаем не «ноль», а «почти ноль».
    """
    floor = max(2, int(extent * ratio))
    return [count > floor for count in counts]


def find_boxes(width, height, rows):
    """Прямоугольники стикеров в порядке чтения: сверху вниз, слева направо."""
    found = components(width, height, rows)
    if not found:
        return []
    largest = max(area for area, *_ in found)
    big = [(left, top, right, bottom)
           for area, left, top, right, bottom in found if area > largest * 0.15]
    small = [b for b in found if b[0] <= largest * 0.15]
    boxes = attach_small(big, small)

    # Порядок чтения: сначала по рядам, внутри ряда слева направо. Ряд
    # определяем по середине стикера, округлённой до типичной высоты.
    heights = sorted(bottom - top for _left, top, _right, bottom in boxes)
    line_height = max(1, heights[len(heights) // 2])
    boxes.sort(key=lambda b: (int((b[1] + b[3]) / 2 / line_height), b[0]))
    boxes = split_wide(rows, boxes)
    boxes.sort(key=lambda b: (int((b[1] + b[3]) / 2 / line_height), b[0]))
    return [tighten(rows, *b) for b in boxes]


def split_wide(rows, boxes, factor=1.4):
    """Разрезает слипшиеся стикеры по самым «пустым» столбцам.

    Соседи на листе часто соприкасаются блёстками, и связная область
    захватывает двоих-троих сразу. Такой бокс заметно шире остальных:
    делим его на столько частей, во сколько он шире типичного.
    """
    widths = sorted(right - left for left, _top, right, _bottom in boxes)
    typical = widths[len(widths) // 2] if widths else 1
    out = []
    for left, top, right, bottom in boxes:
        width = right - left
        parts = int(round(width / typical)) if typical else 1
        if width <= typical * factor or parts < 2:
            out.append((left, top, right, bottom))
            continue
        counts = [sum(1 for line in rows[top:bottom]
                      if line[x * 4 + 3] > ALPHA_FLOOR)
                  for x in range(left, right)]
        cuts = []
        for part in range(1, parts):
            centre = width * part // parts
            window = max(8, int(typical * 0.22))
            lo, hi = max(1, centre - window), min(width - 1, centre + window)
            cuts.append(left + min(range(lo, hi), key=lambda i: counts[i]))
        edges = [left] + sorted(cuts) + [right]
        for index in range(len(edges) - 1):
            out.append((edges[index], top, edges[index + 1], bottom))
    return out


def tighten(rows, left, top, right, bottom):
    """Ужимает прямоугольник вплотную к непрозрачным пикселям."""
    while top < bottom and not any(rows[top][x * 4 + 3] > ALPHA_FLOOR
                                   for x in range(left, right)):
        top += 1
    while bottom > top and not any(rows[bottom - 1][x * 4 + 3] > ALPHA_FLOOR
                                   for x in range(left, right)):
        bottom -= 1
    while left < right and not any(rows[y][left * 4 + 3] > ALPHA_FLOOR
                                   for y in range(top, bottom)):
        left += 1
    while right > left and not any(rows[y][(right - 1) * 4 + 3] > ALPHA_FLOOR
                                   for y in range(top, bottom)):
        right -= 1
    return left, top, right, bottom


# --------------------------------------------------------------------------- #
# Масштабирование
# --------------------------------------------------------------------------- #

def scale_area(rows, box, out_w, out_h):
    """Уменьшение усреднением по площади: для даунскейла даёт мягкий результат."""
    left, top, right, bottom = box
    src_w, src_h = right - left, bottom - top
    out = []
    for y in range(out_h):
        y0 = top + y * src_h // out_h
        y1 = max(y0 + 1, top + (y + 1) * src_h // out_h)
        line = bytearray(out_w * 4)
        for x in range(out_w):
            x0 = left + x * src_w // out_w
            x1 = max(x0 + 1, left + (x + 1) * src_w // out_w)
            r = g = b = a = n = 0
            for sy in range(y0, y1):
                row = rows[sy]
                for sx in range(x0, x1):
                    i = sx * 4
                    alpha = row[i + 3]
                    # цвет взвешиваем по альфе, иначе прозрачные пиксели
                    # «подмешивают» чёрный и по краю появляется кайма
                    r += row[i] * alpha
                    g += row[i + 1] * alpha
                    b += row[i + 2] * alpha
                    a += alpha
                    n += 1
            i = x * 4
            if a:
                line[i] = min(255, r // a)
                line[i + 1] = min(255, g // a)
                line[i + 2] = min(255, b // a)
                line[i + 3] = a // n
        out.append(line)
    return out


def to_canvas(rows, box, size=CANVAS, pad=PAD):
    """Вписывает стикер в прозрачный квадрат заданного размера."""
    left, top, right, bottom = box
    inner = int(size * (1 - 2 * pad))
    src_w, src_h = right - left, bottom - top
    if src_w >= src_h:
        out_w = inner
        out_h = max(1, round(inner * src_h / src_w))
    else:
        out_h = inner
        out_w = max(1, round(inner * src_w / src_h))

    scaled = scale_area(rows, box, out_w, out_h)
    canvas = [bytearray(size * 4) for _ in range(size)]
    off_x, off_y = (size - out_w) // 2, (size - out_h) // 2
    for y, line in enumerate(scaled):
        canvas[off_y + y][off_x * 4:(off_x + out_w) * 4] = line
    return canvas


# --------------------------------------------------------------------------- #

def contact_sheet(folder, cell=140, cols=6):
    """Собирает все 512×512 из папки в один лист — удобно проверять глазами."""
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".png")
                   and not f.startswith("_"))
    if not files:
        print("В %s нет PNG." % folder)
        return 1
    lines = (len(files) + cols - 1) // cols
    width, height = cell * cols, cell * lines
    canvas = [bytearray(width * 4) for _ in range(height)]
    for line in canvas:                       # серый фон: белая обводка видна
        for x in range(width):
            line[x * 4:x * 4 + 4] = bytes((210, 214, 220, 255))

    for index, name in enumerate(files):
        src_w, src_h, rows = read_png(os.path.join(folder, name))
        small = scale_area(rows, (0, 0, src_w, src_h), cell - 8, cell - 8)
        off_x, off_y = (index % cols) * cell + 4, (index // cols) * cell + 4
        for y, line in enumerate(small):
            dst = canvas[off_y + y]
            for x in range(cell - 8):
                alpha = line[x * 4 + 3]
                if alpha:
                    i, j = (off_x + x) * 4, x * 4
                    for c in range(3):
                        dst[i + c] = (line[j + c] * alpha
                                      + dst[i + c] * (255 - alpha)) // 255

    out = os.path.join(folder, "_contact.png")
    write_png(out, width, height, canvas)
    print("лист собран: %s (%d стикеров, по %d в ряд)" % (out, len(files), cols))
    for index, name in enumerate(files, 1):
        print("  %2d. %s" % (index, name))
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--contact" in sys.argv:
        return contact_sheet(args[0] if args else "stiker_pack/ready")
    if not args:
        print(__doc__)
        return 1
    source = args[0]
    out_dir = "stiker_pack/ready"
    if "--out" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out") + 1]
    os.makedirs(out_dir, exist_ok=True)

    print("читаю %s …" % source)
    width, height, rows = read_png(source)
    print("  %dx%d" % (width, height))

    boxes = find_boxes(width, height, rows)
    print("найдено стикеров: %d" % len(boxes))
    if not boxes:
        print("Ничего не нашёл. Фон должен быть прозрачным, а стикеры — "
              "не соприкасаться друг с другом.")
        return 1

    for index, box in enumerate(boxes, 1):
        left, top, right, bottom = box
        canvas = to_canvas(rows, box)
        path = os.path.join(out_dir, "%02d.png" % index)
        write_png(path, CANVAS, CANVAS, canvas)
        print("  %02d  %4dx%-4d -> %s  %d КБ"
              % (index, right - left, bottom - top, path,
                 os.path.getsize(path) // 1024))

    print("\nГотово. Дальше: назовите файлы по событиям или запустите\n"
          "  python get_sticker_ids.py --upload %s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
