#!/usr/bin/env python3
# Reparar fotos 1.1 · Copyright (c) 2026 David Ramos · SPDX-License-Identifier: MIT
# Se ofrece tal cual, sin garantías de ningún tipo; el texto completo de la licencia está en el archivo LICENSE.
# Si redistribuyes o modificas este código, conserva este aviso, como exige la licencia MIT.
"""
Repara fotos JPG de cámara que en la miniatura se ven bien, pero al abrirlas muestran una franja
y el resto gris, porque una parte de los datos del archivo se dañó.

Uso:
	python3 reparar_fotos.py CARPETA_O_FOTOS... [--salida CARPETA] [--procesos N]

Ejemplo:
	python3 ~/reparar-fotos/reparar_fotos.py ~/Descargas/fotos-dañadas

- No modifica los originales. Las reparadas se guardan con el mismo nombre en la carpeta de salida
  (por omisión, "recuperadas" dentro de la carpeta de las fotos), junto con "reporte.txt".
- Todo es local: solo usa Python y Pillow, sin conexión a internet.
- Conserva los datos de la cámara (Exif, XMP y la vista previa interna) y pone al archivo la fecha de captura.
- La parte sana se copia tal cual, sin recomprimir. Lo perdido se rellena con la vista previa interna
  de la cámara, así que esa franja queda menos nítida.
"""
import argparse
import io
import math
import os
import re
import statistics
import struct
import sys
import time
from array import array
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageChops, ImageFile, ImageStat

ALINEACION = 4096  # el daño llega en bloques; sus bordes se redondean a este tamaño
UNION = 8192  # marcas de daño separadas por menos que esto forman una sola zona
MAX_PIXELES = 250_000_000  # ninguna cámara llega a esto; protege de archivos con dimensiones falsas
MAX_BYTES = 300 * 1024 * 1024  # una foto de cámara pesa mucho menos
FFD8, FFD9 = b'\xff\xd8', b'\xff\xd9'
ZZ = [0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
	35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63]
CS = [[(math.sqrt(0.5) if f == 0 else 1.0) * 0.5 * math.cos((2 * x + 1) * f * math.pi / 16) for x in range(8)] for f in range(8)]


class NoReparable(Exception):
	pass


def tiff(t):
	e = '<' if t[:2] == b'II' else '>'
	u16 = lambda x: struct.unpack(e + 'H', t[x:x + 2])[0]
	u32 = lambda x: struct.unpack(e + 'I', t[x:x + 4])[0]

	def ifd(x):
		n = u16(x)
		tags = {u16(x + 2 + 12 * k): (u16(x + 4 + 12 * k), u32(x + 6 + 12 * k), x + 10 + 12 * k) for k in range(n)}
		return tags, u32(x + 2 + 12 * n)

	return e, u16, u32, ifd


def tabla_huffman(cuentas, simbolos):
	S, L, codigos = [0] * 65536, [0] * 65536, {}
	code = k = 0
	for n in range(1, 17):
		for _ in range(cuentas[n - 1]):
			s = simbolos[k]
			k += 1
			codigos[s] = (code, n)
			a, ancho = code << (16 - n), 1 << (16 - n)
			S[a:a + ancho] = [s] * ancho
			L[a:a + ancho] = [n] * ancho
			code += 1
		code <<= 1
	return S, L, codigos


def abrir(b):
	ImageFile.LOAD_TRUNCATED_IMAGES = False
	im = Image.open(io.BytesIO(b))
	im.load()
	return im


def abre_bien(b):
	try:
		abrir(b)
		return True
	except Exception:
		return False


class Foto:
	def __init__(self, d):
		self.d = d
		if d[:2] != FFD8:
			raise NoReparable('el archivo no es un JPG válido')
		i, self.segs = 2, []
		while True:
			if i + 4 > len(d) or d[i] != 0xFF:
				raise NoReparable('el encabezado del archivo está dañado')
			m = d[i + 1]
			if m == 0xFF:
				i += 1
				continue
			largo = struct.unpack('>H', d[i + 2:i + 4])[0]
			self.segs.append((i, m, d[i:i + 2 + largo]))
			i += 2 + largo
			if m == 0xDA:
				break
		self.ini_datos = i
		self.qt, self.comps, huff, sos, dri, sof = {}, [], {}, [], 0, None
		for _, m, s in self.segs:
			seg = s[4:]
			if m == 0xDB:
				p = 0
				while p < len(seg):
					pq, tq = seg[p] >> 4, seg[p] & 15
					p += 1
					self.qt[tq] = list(struct.unpack('>64H', seg[p:p + 128])) if pq else list(seg[p:p + 64])
					p += 128 if pq else 64
			elif m in (0xC0, 0xC1):
				if seg[0] != 8:
					raise NoReparable('la profundidad de color no es compatible')
				self.alto, self.ancho = struct.unpack('>HH', seg[1:5])
				for k in range(seg[5]):
					self.comps.append({'id': seg[6 + 3 * k], 'h': seg[7 + 3 * k] >> 4, 'v': seg[7 + 3 * k] & 15, 'tq': seg[8 + 3 * k]})
				sof = m
			elif m in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
				raise NoReparable('es un JPG progresivo o con otra codificación, que este método no admite')
			elif m == 0xC4:
				p = 0
				while p < len(seg):
					tc, th = seg[p] >> 4, seg[p] & 15
					cuentas = list(seg[p + 1:p + 17])
					p += 17
					huff[(tc, th)] = (cuentas, list(seg[p:p + sum(cuentas)]))
					p += sum(cuentas)
			elif m == 0xDD:
				dri = struct.unpack('>H', seg[:2])[0]
			elif m == 0xDA:
				for k in range(seg[0]):
					sos.append((seg[1 + 2 * k], seg[2 + 2 * k] >> 4, seg[2 + 2 * k] & 15))
		if sof is None:
			raise NoReparable('el archivo no indica las dimensiones de la imagen')
		if len(self.comps) != 3:
			raise NoReparable('este método solo admite fotos a color')
		if dri:
			raise NoReparable('usa marcadores de reinicio, que este método no admite')
		if len(sos) != 3:
			raise NoReparable('usa varios escaneos, que este método no admite')
		self.tab = {k: tabla_huffman(*v) for k, v in huff.items()}
		self.hmax = max(c['h'] for c in self.comps)
		self.vmax = max(c['v'] for c in self.comps)
		if self.ancho * self.alto > MAX_PIXELES:
			raise NoReparable('indica más de 250 megapíxeles, una dimensión que no corresponde a una foto de cámara')
		self.por_fila = math.ceil(self.ancho / (8 * self.hmax))
		self.filas = math.ceil(self.alto / (8 * self.vmax))
		self.slots, self.pos_slot, self.rapido = [], [], []
		for cs, td, ta in sos:
			c = next(k for k, cc in enumerate(self.comps) if cc['id'] == cs)
			if (0, td) not in self.tab or (1, ta) not in self.tab:
				raise NoReparable('faltan las tablas de Huffman')
			h, v = self.comps[c]['h'], self.comps[c]['v']
			for j in range(h * v):
				self.slots.append((c, td, ta))
				self.pos_slot.append((c, h, v, j % h, j // h))
				self.rapido.append((c, *self.tab[(0, td)][:2], *self.tab[(1, ta)][:2]))
		self.ns = len(self.slots)
		self.total = self.por_fila * self.filas * self.ns

	def posicion(self, b):
		mcu, slot = divmod(b, self.ns)
		fila, col = divmod(mcu, self.por_fila)
		c, h, v, jx, jy = self.pos_slot[slot]
		return c, col * h + jx, fila * v + jy

	def decodificar(self, u, pos, slot, nbits, limite=None, inicios=None, dcs=None):
		"""Lee bloques desde el bit `pos`. Devuelve (bloques, bit final, motivo de parada)."""
		ns, rapido, fb = self.ns, self.rapido, int.from_bytes
		preds = [0, 0, 0]
		nb = 0
		while limite is None or nb < limite:
			if pos >= nbits:
				return nb, pos, 'fin'
			c, dS, dL, aS, aL = rapido[slot]
			p0 = pos
			by = pos >> 3
			idx = (fb(u[by:by + 3], 'big') >> (8 - (pos & 7))) & 0xFFFF
			n = dL[idx]
			if not n:
				return nb, p0, 'código DC inválido'
			s = dS[idx]
			pos += n
			if s > 11:
				return nb, p0, 'categoría DC inválida'
			if s:
				by = pos >> 3
				bits = ((fb(u[by:by + 3], 'big') >> (8 - (pos & 7))) & 0xFFFF) >> (16 - s)
				if bits < (1 << (s - 1)):
					bits -= (1 << s) - 1
				pos += s
			else:
				bits = 0
			k = 1
			while k < 64:
				by = pos >> 3
				idx = (fb(u[by:by + 3], 'big') >> (8 - (pos & 7))) & 0xFFFF
				n = aL[idx]
				if not n:
					return nb, p0, 'código AC inválido'
				rs = aS[idx]
				pos += n
				s2 = rs & 15
				if s2 == 0:
					if rs == 0xF0:
						k += 16
						continue
					if rs == 0:
						break
					return nb, p0, 'símbolo AC inválido'
				k += (rs >> 4) + 1
				pos += s2
			if k > 64:
				return nb, p0, 'más de 64 coeficientes'
			if pos > nbits:
				return nb, p0, 'fin'
			if inicios is not None:
				inicios.append(p0)
			preds[c] += bits
			if dcs is not None:
				dcs.append(preds[c])
			nb += 1
			slot = slot + 1 if slot + 1 < ns else 0
		return nb, pos, None

	def exif(self):
		"""Fecha de captura (con zona horaria) y miniatura del Exif."""
		seg = next((s for _, m, s in self.segs if m == 0xE1 and s[4:10] == b'Exif\x00\x00'), None)
		if seg is None:
			return None, None
		try:
			t = seg[10:]
			e, u16, u32, ifd = tiff(t)
			i0, sig = ifd(u32(4))

			def txt(tags, tag):
				if tag not in tags:
					return None
				_, n, vo = tags[tag]
				p = vo if n <= 4 else u32(vo)
				return t[p:p + n].rstrip(b'\x00').decode('latin-1').strip()

			fecha = None
			if 0x8769 in i0:
				ex, _ = ifd(u32(i0[0x8769][2]))
				f = txt(ex, 0x9003)
				if f:
					fecha = datetime.strptime(f, '%Y:%m:%d %H:%M:%S')
					ss = txt(ex, 0x9291)
					if ss and ss.isdigit():
						fecha += timedelta(seconds=float('0.' + ss))
					off = txt(ex, 0x9011)
					if off and re.fullmatch(r'[+-]\d\d:\d\d', off):
						sg = -1 if off[0] == '-' else 1
						fecha = fecha.replace(tzinfo=timezone(sg * timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))))
					else:
						fecha = fecha.astimezone()
			mini = None
			if sig:
				i1, _ = ifd(sig)
				if 0x201 in i1 and 0x202 in i1:
					ent = lambda tag: u32(i1[tag][2]) if i1[tag][0] == 4 else u16(i1[tag][2])
					o, n = ent(0x201), ent(0x202)
					mini = t[o:o + n]
			return fecha, mini
		except Exception:
			return None, None

	def mpf(self):
		"""Lista de imágenes del segmento MPF: (posición dentro del segmento, atributo, tamaño, inicio en el archivo)."""
		for i, m, s in self.segs:
			if m == 0xE2 and s[4:8] == b'MPF\x00':
				try:
					t = s[8:]
					e, u16, u32, ifd = tiff(t)
					tags, _ = ifd(u32(4))
					_, n, vo = tags[0xB002]
					vo = u32(vo)
					ents = []
					for q in range(n // 16):
						attr, size, off = struct.unpack(e + 'III', t[vo + 16 * q:vo + 16 * q + 12])
						ents.append((8 + vo + 16 * q, attr, size, i + 8 + off if off else 0))
					return i, e, ents
				except Exception:
					return None
		return None


def buscar_zonas(d, ini, fin):
	"""Zonas dañadas: un FF que no va seguido de 00 o tramos largos de ceros."""
	marcas = []
	j = ini
	while True:
		j = d.find(b'\xff', j, fin)
		if j < 0 or j + 1 >= fin:
			break
		if d[j + 1] == 0x00:
			j += 2
			continue
		marcas.append((j, j + 2))
		j += 1
	for m in re.finditer(rb'\x00{256,}', d[ini:fin]):
		marcas.append((ini + m.start(), ini + m.end()))
	marcas.sort()
	juntas = []
	for a, b in marcas:
		if juntas and a - juntas[-1][1] <= UNION:
			juntas[-1][1] = max(juntas[-1][1], b)
		else:
			juntas.append([a, b])
	zonas = []
	for a, b in juntas:
		a, b = max(ini, a // ALINEACION * ALINEACION), min(fin, -(-b // ALINEACION) * ALINEACION)
		if zonas and a <= zonas[-1][1]:
			zonas[-1][1] = max(zonas[-1][1], b)
		else:
			zonas.append([a, b])
	return zonas


def a_original(d, a, b, k):
	"""Posición en el archivo del byte k de los datos del tramo [a, b) ya sin bytes de relleno."""
	extra = 0
	for m in re.finditer(b'\xff\x00', d[a:b]):
		if m.start() - extra >= k:
			break
		extra += 1
	return a + k + extra


def sin_franjas_negras(im):
	g = im.convert('L')
	w, h = g.size

	def oscura(caja):
		parte = g.crop(caja)
		return ImageStat.Stat(parte).mean[0] < 16 and parte.getextrema()[1] < 48

	arriba, abajo, izq, der = 0, h, 0, w
	while arriba < h // 4 and oscura((0, arriba, w, arriba + 1)):
		arriba += 1
	while abajo > h - h // 4 and oscura((0, abajo - 1, w, abajo)):
		abajo -= 1
	while izq < w // 4 and oscura((izq, 0, izq + 1, h)):
		izq += 1
	while der > w - w // 4 and oscura((der - 1, 0, der, h)):
		der -= 1
	return im.crop((izq, arriba, der, abajo))


def referencia(f, mpf):
	"""La vista previa interna más grande que abra completa; si no hay, la miniatura del Exif.
	Solo cuentan las imágenes marcadas como vista previa (tipos 0x010001 y 0x010002 del MPF): otras imágenes
	internas, como los mapas de brillo del iPhone, no sirven para el color."""
	aspecto = f.ancho / f.alto
	extras, cands = [], []
	if mpf:
		for _, attr, size, off in mpf[2][1:]:
			ok = False
			if off and off + size <= len(f.d) and f.d[off:off + 2] == FFD8:
				try:
					im = abrir(f.d[off:off + size])
					ok = True
					if attr & 0xFFFFFF in (0x010001, 0x010002) and abs(im.size[0] / im.size[1] - aspecto) / aspecto < 0.02:
						cands.append((im.size[0] * im.size[1], im))
				except Exception:
					pass
			extras.append(ok)
	if cands:
		return max(cands, key=lambda x: x[0])[1].convert('YCbCr'), 'vista previa interna', extras
	_, mini = f.exif()
	if mini:
		try:
			im = sin_franjas_negras(abrir(mini).convert('RGB'))
			if abs(im.size[0] / im.size[1] - aspecto) / aspecto < 0.06:
				return im.convert('YCbCr'), 'miniatura del Exif', extras
		except Exception:
			pass
	return None, None, extras


def rejillas(f, ref):
	"""Valor DC esperado de cada bloque según la referencia."""
	out = []
	for c, (cc, p) in enumerate(zip(f.comps, ref.split())):
		w, h = f.por_fila * cc['h'], f.filas * cc['v']
		g = p.resize((w, h), Image.BOX if p.size[0] >= w else Image.BICUBIC)
		out.append((g.tobytes(), w, 8 / f.qt[cc['tq']][0]))
	return out


def paso_parejo(paso, ns):
	"""Un paso de muestreo que no sea múltiplo del número de bloques por grupo, para no saltarse canales."""
	paso = max(1, paso)
	while math.gcd(paso, ns) != 1:
		paso += 1
	return paso


def muestra(n, k, ns):
	a, b = min(64, n // 10), n - min(64, n // 10)
	if b - a <= k:
		return list(range(a, b))
	return list(range(a, b, paso_parejo((b - a) // k, ns)))[:k]


def puntaje(f, grids, s, b_abs, idxs):
	"""Qué tan bien coincide un tramo con la referencia si empieza en el bloque b_abs (menor es mejor)."""
	res = ([], [], [])
	dcs = s['dcs']
	for i in idxs:
		b = b_abs + i
		if b < 0 or b >= f.total:
			continue
		c, bx, by = f.posicion(b)
		g = grids[c]
		res[c].append((g[0][by * g[1] + bx] - 128) * g[2] - dcs[i])
	if not all(res):
		return float('inf'), [0, 0, 0], [0, 0, 0]
	P = [statistics.median(x) for x in res]
	mad = [statistics.median([abs(v - p) for v in x]) for x, p in zip(res, P)]
	return sum(mad), P, mad


def resincronizar(f, up, nbits):
	"""Primer bloque sano después de un daño: el punto por el que pasan todas (o casi todas) las lecturas de prueba.
	Se prueban arranques en los primeros 256 bits para que alguno caiga en el inicio real de un bloque."""
	conteo, validos = Counter(), 0
	for b0 in range(min(256, nbits)):
		for s0 in range(f.ns):
			ini = array('Q')
			nb, _, motivo = f.decodificar(up, b0, s0, nbits, limite=300, inicios=ini)
			if motivo is None or (motivo == 'fin' and nb):
				validos += 1
				conteo.update((p, (s0 + k) % f.ns) for k, p in enumerate(ini))
	for umbral in (1.0, 0.9, 0.75):
		comunes = [k for k, v in conteo.items() if v >= umbral * validos]
		if comunes:
			return min(comunes)
	return None


def ubicar(f, grids, segs, i, ini_datos, fin_datos, rep):
	"""Ubica un tramo intermedio comparándolo con la referencia en las posiciones posibles."""
	s = segs[i]
	prev = next((segs[q] for q in range(i - 1, -1, -1) if segs[q]['b_abs'] is not None), None)
	sig = next((q for q in range(i + 1, len(segs)) if segs[q]['b_abs'] is not None), None)
	L = prev['b_abs'] + prev['n'] if prev else 0
	fin = segs[sig]['b_abs'] if sig is not None else f.total
	U = fin - s['n'] - sum(segs[q]['n'] for q in range(i + 1, sig if sig is not None else len(segs)))
	if prev and prev['b'] > prev['a']:
		dens = prev['n'] / (prev['b'] - prev['a'])
	else:
		dens = f.total / max(1, fin_datos - ini_datos)
	perdido = (s['a'] - (prev['b'] if prev else ini_datos)) * dens
	centro, W = L + perdido, max(2 * f.por_fila * f.ns, 0.75 * perdido)
	lo, hi = max(L, int(centro - W)), min(U, int(centro + W))
	cands = list(range(lo + (s['s0'] - lo) % f.ns, hi + 1, f.ns))
	if not cands or s['n'] < 200:
		rep['avisos'].append(f'no fue posible ubicar un tramo de {s["n"]:,} bloques; se rellenó')
		return
	idx_g = muestra(s['n'], 150, f.ns)
	gruesos = {b: puntaje(f, grids, s, b, idx_g)[0] for b in cands}
	mejores = sorted(cands, key=gruesos.get)[:25]
	idx_f = muestra(s['n'], 900, f.ns)
	finos = sorted((puntaje(f, grids, s, b, idx_f)[0], b) for b in mejores)
	mejor = finos[0][1]
	lejos = [v for b, v in gruesos.items() if abs(b - mejor) > 2 * f.por_fila * f.ns]
	if lejos and min(lejos) < 1.25 * gruesos[mejor]:
		rep['avisos'].append(f'la posición de un tramo de {s["n"]:,} bloques era incierta; se rellenó')
		return
	s['b_abs'] = mejor


def recortar(f, grids, s, cabeza, cola):
	"""Quita de las orillas del tramo los bloques que no coinciden con la referencia (basura leída antes de resincronizar)."""
	n = s['n']
	_, Pm, mad = puntaje(f, grids, s, s['b_abs'], muestra(n, 900, f.ns))
	T = [max(6 * m, 4.0) for m in mad]
	dcs = s['dcs']

	def bueno(i):
		b = s['b_abs'] + i
		if b >= f.total:
			return False
		c, bx, by = f.posicion(b)
		g = grids[c]
		return abs((g[0][by * g[1] + bx] - 128) * g[2] - dcs[i] - Pm[c]) <= T[c]

	# cada orilla debe empezar con 4 bloques buenos seguidos y tener al menos 13 de 16 buenos
	k0, k1 = 0, n
	if cabeza:
		lim = min(n, 20000)
		ok = [bueno(i) for i in range(lim)]
		k0 = next((i for i in range(max(1, lim - 15)) if all(ok[i:i + 4]) and sum(ok[i:i + 16]) >= 13), n)
	if cola and k0 < n:
		lo = max(k0, n - 20000)
		ok = [bueno(i) for i in range(lo, n)]
		k1 = next((e for e in range(n, lo + 15, -1) if all(ok[e - lo - 4:e - lo]) and sum(ok[e - lo - 16:e - lo]) >= 13), k0)
	return k0, k1, Pm


def recortada(x):
	"""Promedio sin el 25 % más alto ni el 25 % más bajo."""
	x = sorted(x)
	q = len(x) // 4
	return statistics.fmean(x[q:len(x) - q]) if len(x) > 3 else (statistics.fmean(x) if x else 0.0)


def diferencias(f, grids, s, P):
	"""Diferencia entre el valor DC esperado por la referencia y el del tramo (con su corrección P), por canal."""
	res = ([], [], [])
	for i in range(s['k0'], s['k1'], paso_parejo((s['k1'] - s['k0']) // 60000, f.ns)):
		c, bx, by = f.posicion(s['b_abs'] + i)
		g = grids[c]
		res[c].append((g[0][by * g[1] + bx] - 128) * g[2] - s['dcs'][i] - P[c])
	return res


def dct(px):
	T = [[sum(px[y * 8 + x] * CS[f][x] for x in range(8)) for f in range(8)] for y in range(8)]
	return [sum(T[y][fu] * CS[fv][y] for y in range(8)) for fv in range(8) for fu in range(8)]


def puente(f, ref, g0, g1, preds, X):
	"""Codifica los bloques perdidos [g0, g1) a partir de la referencia ampliada. X fija el valor DC del último bloque de cada canal."""
	ns, pf = f.ns, f.por_fila
	r0, r1 = (g0 // ns) // pf, ((g1 - 1) // ns) // pf
	planos = []
	for c, (cc, rp) in enumerate(zip(f.comps, ref.split())):
		W = pf * cc['h'] * 8
		y0, y1 = r0 * cc['v'] * 8, (r1 + 1) * cc['v'] * 8
		cw, ch = math.ceil(f.ancho * cc['h'] / f.hmax), math.ceil(f.alto * cc['v'] / f.vmax)
		sx, sy = rp.size[0] / cw, rp.size[1] / ch
		caja = (0, min(y0 * sy, rp.size[1] - 1), min(W * sx, rp.size[0]), min(y1 * sy, rp.size[1]))
		planos.append((rp.resize((W, y1 - y0), Image.BICUBIC, box=caja).tobytes(), W, y0))
	ultimo = {}
	for b in range(g1 - 1, max(g0, g1 - ns) - 1, -1):
		ultimo.setdefault(f.slots[b % ns][0], b)
	preds = list(preds)
	bits = []
	for b in range(g0, g1):
		c, bx, by = f.posicion(b)
		_, td, ta = f.slots[b % ns]
		data, W, y0 = planos[c]
		base = (by * 8 - y0) * W + bx * 8
		F = dct([data[base + y * W + x] - 128 for y in range(8) for x in range(8)])
		q = f.qt[f.comps[c]['tq']]
		coef = [round(F[ZZ[k]] / q[k]) for k in range(64)]
		if X is not None and ultimo.get(c) == b:
			coef[0] = X[c]
		dif = max(-2047, min(2047, coef[0] - preds[c]))
		preds[c] += dif
		s = abs(dif).bit_length()
		dc = f.tab[(0, td)][2]
		if s not in dc:
			raise NoReparable('la tabla de codificación de la foto está incompleta')
		code, n = dc[s]
		bits.append(format(code, f'0{n}b'))
		if s:
			bits.append(format(dif if dif >= 0 else dif + (1 << s) - 1, f'0{s}b'))
		ac = f.tab[(1, ta)][2]
		corrida = 0
		for k in range(1, 64):
			v = coef[k]
			if v == 0:
				corrida += 1
				continue
			s = min(abs(v).bit_length(), 10)
			sim = ((corrida % 16) << 4) | s
			if sim not in ac or (corrida > 15 and 0xF0 not in ac):
				corrida += 1
				continue
			for _ in range(corrida // 16):
				code, n = ac[0xF0]
				bits.append(format(code, f'0{n}b'))
			code, n = ac[sim]
			bits.append(format(code, f'0{n}b'))
			v = max(min(v, (1 << s) - 1), -((1 << s) - 1))
			bits.append(format(v if v >= 0 else v + (1 << s) - 1, f'0{s}b'))
			corrida = 0
		if corrida:
			code, n = ac[0x00]
			bits.append(format(code, f'0{n}b'))
	cadena = ''.join(bits)
	return (int(cadena, 2) if cadena else 0), len(cadena), preds


def bits_de(u, a, b):
	v = int.from_bytes(u, 'big') >> (len(u) * 8 - b)
	return v & ((1 << (b - a)) - 1), b - a


def base_dc(f, s):
	"""Valor acumulado del último bloque de cada canal antes del primer bloque que se conserva del tramo."""
	base, vistos = [0, 0, 0], set()
	for i in range(s['k0'] - 1, -1, -1):
		c = f.slots[(s['s0'] + i) % f.ns][0]
		if c not in vistos:
			base[c] = s['dcs'][i]
			vistos.add(c)
			if len(vistos) == 3:
				break
	return base


def filas_px(f, g0, g1):
	r0, r1 = (g0 // f.ns) // f.por_fila, ((g1 - 1) // f.ns) // f.por_fila
	return r0 * 8 * f.vmax, min(f.alto, (r1 + 1) * 8 * f.vmax)


def procesar(ruta, carpeta):
	t0 = time.time()
	nombre = os.path.basename(ruta)
	destino = os.path.join(carpeta, nombre)
	tmp = os.path.join(carpeta, '.' + nombre + '.parcial')
	rep = {'foto': nombre, 'estado': '', 'notas': [], 'avisos': []}
	try:
		if os.path.exists(destino):
			rep['estado'] = 'omitida'
			rep['notas'].append('ya existe en la carpeta de salida y no se sobrescribe')
			return rep
		if os.path.getsize(ruta) > MAX_BYTES:
			rep['estado'] = 'sin reparar'
			rep['notas'].append('supera el límite de 300 MB por archivo')
			return rep
		with open(ruta, 'rb') as fh:
			d = fh.read()
		f = Foto(d)
		mpf = f.mpf()
		fin_principal = len(d)
		if mpf and mpf[2] and 0 < mpf[2][0][2] <= len(d) and d[mpf[2][0][2] - 2:mpf[2][0][2]] == FFD9:
			fin_principal = mpf[2][0][2]
		limpio = d[fin_principal - 2:fin_principal] == FFD9
		fin_datos = fin_principal - 2 if limpio else fin_principal
		zonas = buscar_zonas(d, f.ini_datos, fin_datos)
		if not zonas and limpio and abre_bien(d):
			rep['estado'] = 'sin daño'
			rep['notas'].append('la foto está completa; no requiere reparación')
			return rep
		ref, origen, extras_ok = referencia(f, mpf)
		if ref is None:
			raise NoReparable('no contiene una vista previa interna utilizable como referencia')
		if origen != 'vista previa interna':
			rep['avisos'].append('se usó la miniatura de 160×120 como referencia, por lo que el color y la zona reconstruida son menos precisos')
		grids = rejillas(f, ref)

		# 1) leer cada tramo sano; si aparece un error que no dejó marcas, se parte ahí
		tramos, cursor = [], f.ini_datos
		for a, b in zonas:
			if a > cursor:
				tramos.append([cursor, a])
			cursor = max(cursor, b)
		if cursor < fin_datos:
			tramos.append([cursor, fin_datos])
		segs, k = [], 0
		while k < len(tramos):
			a, b = tramos[k]
			u = d[a:b].replace(b'\xff\x00', b'\xff')
			nbits = len(u) * 8
			up = u + b'\x00' * 8
			primero = a == f.ini_datos
			inicio = (0, 0) if primero else resincronizar(f, up, nbits)
			if inicio is None:
				rep['avisos'].append(f'un tramo de {b - a:,} bytes resultó ilegible; se rellenó')
				k += 1
				continue
			ini, dcs = array('Q'), array('i')
			nb, pos, motivo = f.decodificar(up, inicio[0], inicio[1], nbits, inicios=ini, dcs=dcs)
			if motivo != 'fin':
				err = a_original(d, a, b, pos // 8)
				z0 = err // ALINEACION * ALINEACION
				z1 = z0 + ALINEACION
				zonas.append([max(z0, a), min(z1, b)])
				if len(zonas) > 64:
					raise NoReparable('tiene demasiadas zonas dañadas para repararla')
				tramos[k:k + 1] = ([[a, z0]] if z0 > a else []) + ([[z1, b]] if z1 < b else [])
				continue
			segs.append({'a': a, 'b': b, 'u': u, 'nbits': nbits, 's0': inicio[1], 'n': nb, 'ini': ini, 'dcs': dcs, 'fin': pos,
				'primero': primero, 'b_abs': 0 if primero else None})
			k += 1
		zonas.sort()

		# 2) ubicar cada tramo: el primero empieza en el bloque 0; el último se cuenta desde el final
		ultimo = segs[-1] if segs else None
		if ultimo and not ultimo['primero'] and ultimo['b'] == fin_datos and limpio:
			sobran = ultimo['nbits'] - ultimo['fin']
			unos = (1 << sobran) - 1
			b_abs = f.total - ultimo['n']
			if sobran < 8 and (ultimo['u'][-1] & unos) == unos and b_abs >= 0 and b_abs % f.ns == ultimo['s0']:
				ultimo['b_abs'] = b_abs
				idx = muestra(ultimo['n'], 600, f.ns)
				p0 = puntaje(f, grids, ultimo, b_abs, idx)[0]
				vecinas = [puntaje(f, grids, ultimo, b_abs + dl, idx)[0] for dl in (-f.ns, -f.por_fila * f.ns) if b_abs + dl >= 0]
				if vecinas and p0 >= min(vecinas):
					rep['avisos'].append('la posición del último tramo es incierta')
		for i, s in enumerate(segs):
			if s['b_abs'] is None:
				ubicar(f, grids, segs, i, f.ini_datos, fin_datos, rep)

		# 3) quitar orillas dudosas, calcular la corrección de color y dejar espacio entre tramos para fijarla.
		# La vista previa no es idéntica a la foto: si hay un tramo sano grande antes, esa diferencia se mide
		# ahí y se descuenta (promedios recortados); si no, se usa la mediana, que en las pruebas fue más exacta.
		usados, ancla = [], None
		for s in sorted((s for s in segs if s['b_abs'] is not None), key=lambda s: s['b_abs']):
			cola = not (s['b_abs'] + s['n'] == f.total and s['b'] == fin_datos and limpio)
			s['k0'], s['k1'], _ = recortar(f, grids, s, not s['primero'], cola)
			if s['k0'] >= s['k1']:
				rep['avisos'].append('un tramo no coincidió con la referencia; se rellenó')
				continue
			if s['primero']:
				s['P'] = [0, 0, 0]
			else:
				dif0 = diferencias(f, grids, s, [0, 0, 0])
				if ancla and s['k1'] - s['k0'] >= 20000:
					sesgo = [recortada(x) for x in diferencias(f, grids, ancla, ancla['P'])]
					s['P'] = [round(recortada(x) - z) for x, z in zip(dif0, sesgo)]
				else:
					s['P'] = [round(statistics.median(x)) if x else 0 for x in dif0]
			fin_prev = usados[-1]['b_abs'] + usados[-1]['k1'] if usados else 0
			ini_s = s['b_abs'] + s['k0']
			if not s['primero']:
				if ini_s < fin_prev:
					rep['avisos'].append('dos tramos se superponían; se descartó uno')
					continue
				if ini_s - fin_prev < f.ns:
					if usados:
						usados[-1]['k1'] -= f.ns - (ini_s - fin_prev)
					else:
						s['k0'] += f.ns - ini_s
			usados.append(s)
			if s['k1'] - s['k0'] >= 50000:
				ancla = s

		# 4) armar los datos: tramos sanos tal cual + bloques perdidos rellenados con la referencia
		piezas, preds, cursor, franjas = [], [0, 0, 0], 0, []
		for s in usados:
			if s['k0'] >= s['k1']:
				continue
			b_ini = s['b_abs'] + s['k0']
			if b_ini > cursor:
				base = base_dc(f, s)
				v, n, preds = puente(f, ref, cursor, b_ini, preds, [s['P'][c] + base[c] for c in range(3)])
				piezas.append((v, n))
				franjas.append((cursor, b_ini))
			fin_bit = s['ini'][s['k1']] if s['k1'] < s['n'] else s['fin']
			piezas.append(bits_de(s['u'], s['ini'][s['k0']], fin_bit))
			vistos = set()
			for i in range(s['k1'] - 1, s['k0'] - 1, -1):
				c = f.slots[(s['s0'] + i) % f.ns][0]
				if c not in vistos:
					preds[c] = s['P'][c] + s['dcs'][i]
					vistos.add(c)
					if len(vistos) == 3:
						break
			cursor = s['b_abs'] + s['k1']
		if cursor < f.total:
			v, n, preds = puente(f, ref, cursor, f.total, preds, None)
			piezas.append((v, n))
			franjas.append((cursor, f.total))
		total, largo = 0, 0
		for v, n in piezas:
			total = (total << n) | v
			largo += n
		relleno = (-largo) % 8
		total = (total << relleno) | ((1 << relleno) - 1)
		datos = total.to_bytes((largo + relleno) // 8, 'big').replace(b'\xff', b'\xff\x00')

		# 5) misma estructura que el archivo de la cámara: Exif, XMP, MPF y la vista previa interna al final
		mantener = bool(mpf) and limpio and bool(extras_ok) and all(extras_ok)
		cab = [s for i, m, s in f.segs if mantener or not mpf or i != mpf[0]]
		if mpf and not mantener:
			rep['avisos'].append('la vista previa interna estaba dañada o incompleta y no se incluyó en el archivo')
		principal = bytearray(FFD8 + b''.join(cab) + datos + FFD9)
		if mantener:
			i_mpf, e, ents = mpf
			delta = len(principal) - fin_principal
			for k, (pos, _, _, off) in enumerate(ents):
				if k == 0:
					struct.pack_into(e + 'I', principal, i_mpf + pos + 4, len(principal))
				elif off:
					struct.pack_into(e + 'I', principal, i_mpf + pos + 8, struct.unpack_from(e + 'I', d, i_mpf + pos + 8)[0] + delta)
		with open(tmp, 'wb') as fh:
			fh.write(bytes(principal) + (d[fin_principal:] if mantener else b''))

		# 6) comprobar antes de entregar
		ImageFile.LOAD_TRUNCATED_IMAGES = False
		try:
			with Image.open(tmp) as im:
				im.load()
				chica = im.convert('RGB').resize(ref.size, Image.BOX)
				if getattr(im, 'n_frames', 1) > 1:
					im.seek(1)
					im.load()
		except Exception as ex:
			raise NoReparable(f'el resultado no superó la verificación de lectura ({ex})')
		ref_rgb = ref.convert('RGB')
		dif = ImageChops.difference(chica.convert('L'), ref_rgb.convert('L'))
		w, h = ref.size
		bandas = [ImageStat.Stat(dif.crop((0, q * h // 20, w, (q + 1) * h // 20))).mean[0] for q in range(20)]
		medias = [a - b for a, b in zip(ImageStat.Stat(chica).mean, ImageStat.Stat(ref_rgb).mean)]
		peor = max(range(20), key=lambda q: bandas[q])
		if bandas[peor] > max(3 * statistics.median(bandas), 12):
			rep['avisos'].append(f'la zona de las filas {peor * f.alto // 20}–{(peor + 1) * f.alto // 20} difiere notablemente de la vista previa')
		if max(abs(x) for x in medias) > 2:
			rep['avisos'].append('el color general difiere ligeramente de la vista previa')
		perdidos = sum(b - a for a, b in franjas)
		if perdidos > 0.1 * f.total:
			rep['avisos'].append(f'se perdió el {100 * perdidos / f.total:.0f} % de la imagen; esa parte se reconstruyó con la {origen}')
		os.replace(tmp, destino)
		fecha, _ = f.exif()
		t = round(fecha.timestamp() * 1e9) if fecha else os.stat(ruta).st_mtime_ns
		os.utime(destino, ns=(t, t))

		danado = sum(b - a for a, b in zonas)
		if not limpio:
			rep['notas'].append('el archivo está incompleto: faltan los datos del final')
		if zonas:
			rep['notas'].append(f'datos dañados: {danado / 1024:,.0f} KB en {len(zonas)} zona(s), {100 * danado / max(1, fin_datos - f.ini_datos):.1f} % de la imagen')
		for a, b in franjas:
			y0, y1 = filas_px(f, a, b)
			rep['notas'].append(f'zona reconstruida con la {origen}: filas {y0}–{y1} de {f.alto}')
		rep['notas'].append(f'verificación: abre sin errores; diferencia máxima por franja {max(bandas):.1f} y variación de color {max(abs(x) for x in medias):.1f} (de 255)')
		rep['notas'].append('fecha del archivo: la de captura' if fecha else 'fecha del archivo: la del original (la foto no incluye fecha de captura)')
		rep['estado'] = 'por revisar' if rep['avisos'] else 'reparada'
	except NoReparable as ex:
		rep['estado'] = 'sin reparar'
		rep['notas'].append(str(ex))
	except Exception as ex:
		rep['estado'] = 'sin reparar'
		rep['notas'].append(f'error inesperado: {type(ex).__name__}: {ex}')
	finally:
		if os.path.exists(tmp):
			os.remove(tmp)
		rep['segundos'] = round(time.time() - t0)
	return rep


def main():
	ap = argparse.ArgumentParser(description='Repara fotos JPG que se ven con una franja y el resto gris. Todo es local y no toca los originales.')
	ap.add_argument('entradas', nargs='+', help='carpeta con las fotos, o archivos .jpg')
	ap.add_argument('--salida', help='carpeta para las reparadas (por omisión, "recuperadas" junto a las fotos)')
	ap.add_argument('--procesos', type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)), help='fotos que se reparan a la vez')
	a = ap.parse_args()
	fotos = []
	for e in a.entradas:
		if os.path.isdir(e):
			fotos += sorted(os.path.join(e, x) for x in os.listdir(e) if x.lower().endswith(('.jpg', '.jpeg')) and not x.startswith('.'))
		elif os.path.isfile(e):
			fotos.append(e)
		else:
			print(f'No existe la ruta: {e}')
	if not fotos:
		print('No hay fotos .jpg en las rutas indicadas.')
		return 1
	base = a.entradas[0] if os.path.isdir(a.entradas[0]) else os.path.dirname(os.path.abspath(a.entradas[0]))
	carpeta = os.path.abspath(a.salida or os.path.join(base, 'recuperadas'))
	os.makedirs(carpeta, exist_ok=True)
	print(f'{len(fotos)} foto(s). Carpeta de destino: {carpeta}')
	resultados = []
	with ProcessPoolExecutor(max_workers=max(1, a.procesos)) as ex:
		for fu in as_completed([ex.submit(procesar, os.path.abspath(p), carpeta) for p in fotos]):
			r = fu.result()
			resultados.append(r)
			print(f'  {r["foto"]}: {r["estado"]} ({r["segundos"]} s)')
			for x in r['avisos']:
				print(f'      revisar: {x}')
	resultados.sort(key=lambda r: r['foto'])
	with open(os.path.join(carpeta, 'reporte.txt'), 'a', encoding='utf-8') as fh:
		fh.write(f'\nReparación del {datetime.now():%Y-%m-%d %H:%M}\n')
		for r in resultados:
			fh.write(f'{r["foto"]}: {r["estado"]}\n')
			for x in r['notas']:
				fh.write(f'  - {x}\n')
			for x in r['avisos']:
				fh.write(f'  - revisar: {x}\n')
	resumen = Counter(r['estado'] for r in resultados)
	print('Resumen: ' + ', '.join(f'{v} {k}' for k, v in sorted(resumen.items())))
	print(f'Detalles en {os.path.join(carpeta, "reporte.txt")}')
	return 0


if __name__ == '__main__':
	sys.exit(main())
