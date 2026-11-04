//! locos_native: Rust twins of measured Python hot paths in Localization OS (ADR 0091).
//!
//! Contract: each function returns exactly what its Python reference returns. The fast path
//! takes an exact `str` or None; any other argument (a str subclass, another type, or a lone
//! surrogate where the reference would raise) goes to the Python reference that
//! app.services.native registered with `_set_reference`, so results AND exceptions match.
//!
//! Strings are read from CPython's own storage (PEP 393: 1, 2 or 4 bytes per code point) and
//! processed as code points, the unit Python indexes by. Nothing goes through UTF-8 except the
//! SHA-256 input, and no UTF-8 copy is left cached on the caller's strings. That needs the full
//! (non-abi3) C API: the module is built per CPython minor version.

use std::borrow::Cow;
use std::sync::Mutex;

use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyInt, PyString, PyStringData};

mod simd;
mod split;
mod stamps;
mod tables;
mod tags;
mod text;

static REFS: Mutex<Vec<(String, Py<PyAny>)>> = Mutex::new(Vec::new());

/// Register the Python reference for `name` (the fallback for non-fast-path arguments).
#[pyfunction]
fn _set_reference(name: String, func: Py<PyAny>) {
    let mut refs = REFS.lock().unwrap_or_else(|e| e.into_inner());
    refs.retain(|(n, _)| *n != name);
    refs.push((name, func));
}

fn reference(py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
    let refs = REFS.lock().unwrap_or_else(|e| e.into_inner());
    match refs.iter().find(|(n, _)| n == name) {
        Some((_, f)) => Ok(f.clone_ref(py)),
        None => Err(PyTypeError::new_err(format!(
            "locos_native.{name}: argument types outside the fast path need the Python reference \
             (import app.services first)"
        ))),
    }
}

/// An exact str (not a subclass), else None.
fn exact_str<'a, 'py>(obj: &'a Bound<'py, PyAny>) -> Option<&'a Bound<'py, PyString>> {
    if obj.is_exact_instance_of::<PyString>() {
        obj.cast::<PyString>().ok()
    } else {
        None
    }
}

/// The storage of an exact str.
fn units<'a>(s: &'a Bound<'_, PyString>) -> Option<PyStringData<'a>> {
    // SAFETY: the data is only read while `s` is borrowed, and str objects are immutable.
    unsafe { s.data() }.ok()
}

/// Exact str -> its code points (borrowed when stored 4 bytes wide); None -> empty; anything
/// else -> None (use the reference).
fn code_points<'a>(obj: &'a Bound<'_, PyAny>) -> Option<Cow<'a, [u32]>> {
    if obj.is_none() {
        return Some(Cow::Borrowed(&[]));
    }
    Some(match units(exact_str(obj)?)? {
        PyStringData::Ucs1(b) => Cow::Owned(b.iter().map(|&c| c as u32).collect()),
        PyStringData::Ucs2(w) => Cow::Owned(w.iter().map(|&c| c as u32).collect()),
        PyStringData::Ucs4(d) => Cow::Borrowed(d),
    })
}

/// Append-only scratch space with a capacity fixed up front (the caller passes an upper bound).
/// Typical segment-sized inputs use a 4 KiB area on the stack: on Windows a heap alloc + free
/// cost about as much as normalizing a short string.
struct Writer<T> {
    ptr: *mut T,
    len: usize,
    #[cfg(debug_assertions)]
    cap: usize,
}

impl<T: Copy> Writer<T> {
    #[inline(always)]
    fn push(&mut self, v: T) {
        #[cfg(debug_assertions)]
        assert!(self.len < self.cap);
        // SAFETY: the caller of with_scratch bounded every push by the capacity it asked for.
        unsafe { self.ptr.add(self.len).write(v) };
        self.len += 1;
    }
    #[inline(always)]
    fn as_slice(&self) -> &[T] {
        // SAFETY: the first `len` elements were written by push.
        unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
    }
}

impl<T: Copy> simd::Sink<T> for Writer<T> {
    #[inline(always)]
    fn push(&mut self, v: T) {
        Writer::push(self, v);
    }
    #[inline(always)]
    fn push_slice(&mut self, s: &[T]) {
        #[cfg(debug_assertions)]
        assert!(self.len + s.len() <= self.cap);
        // SAFETY: bounded by the capacity the caller of with_scratch asked for; `s` never
        // overlaps the scratch area.
        unsafe { std::ptr::copy_nonoverlapping(s.as_ptr(), self.ptr.add(self.len), s.len()) };
        self.len += s.len();
    }
}

fn with_scratch<T: Copy, R>(cap: usize, f: impl FnOnce(&mut Writer<T>) -> R) -> R {
    const STACK_BYTES: usize = 4096;
    if cap * std::mem::size_of::<T>() <= STACK_BYTES {
        let mut stack = std::mem::MaybeUninit::<[u64; STACK_BYTES / 8]>::uninit();
        let mut w = Writer {
            ptr: stack.as_mut_ptr() as *mut T,
            len: 0,
            #[cfg(debug_assertions)]
            cap,
        };
        f(&mut w)
    } else {
        let mut heap: Vec<T> = Vec::with_capacity(cap);
        let mut w = Writer {
            ptr: heap.as_mut_ptr(),
            len: 0,
            #[cfg(debug_assertions)]
            cap,
        };
        f(&mut w)
    }
}

/// Write `" ".join(units.split())` into a fresh str, or None when `units` is already in that
/// form (the caller then returns the same str object).
fn collapse<'py, T: simd::Unit>(
    py: Python<'py>,
    units: &[T],
    ascii: bool,
) -> Option<PyResult<Bound<'py, PyAny>>> {
    if simd::is_joined(units) {
        return None;
    }
    // Build in the input's width (the output is never longer than the input), copying the runs
    // between changed units whole, then hand CPython the canonical (narrowest) kind for the
    // widest kept code point.
    Some(with_scratch(units.len(), |buf: &mut Writer<T>| {
        let (mut len, mut last_space) = (0usize, false);
        simd::join_into(units, buf, &mut len, &mut last_space);
        if last_space {
            buf.len -= 1; // the trailing separator
        }
        let out = buf.as_slice();
        // An ASCII input can only give ASCII; otherwise removing whitespace may narrow the kind.
        let maxc = if ascii {
            0x7F
        } else {
            out.iter().map(|&c| c.into()).max().unwrap_or(0)
        };
        // SAFETY: PyUnicode_New(len, maxc) returns a writable, unshared string whose kind fits
        // every code point in `out`; it is filled completely before Python sees it.
        unsafe {
            let obj = ffi::PyUnicode_New(out.len() as ffi::Py_ssize_t, maxc);
            if obj.is_null() {
                return Err(PyErr::fetch(py));
            }
            let kind = ffi::PyUnicode_KIND(obj) as usize;
            let data = ffi::PyUnicode_DATA(obj);
            if kind == std::mem::size_of::<T>() {
                std::ptr::copy_nonoverlapping(out.as_ptr(), data as *mut T, out.len());
            } else {
                for (k, &c) in out.iter().enumerate() {
                    let v: u32 = c.into();
                    match kind {
                        1 => *(data as *mut u8).add(k) = v as u8,
                        2 => *(data as *mut u16).add(k) = v as u16,
                        _ => *(data as *mut u32).add(k) = v,
                    }
                }
            }
            Ok(Bound::from_owned_ptr(py, obj))
        }
    }))
}

/// tm_common.normalize: `" ".join((text or "").split())`.
fn normalize<'py>(py: Python<'py>, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if text.is_none() {
        return Ok(PyString::new(py, "").into_any());
    }
    let Some(s) = exact_str(text) else {
        return reference(py, "normalize")?.bind(py).call1((text,));
    };
    // Lone surrogates are fine here: str.split() keeps them, and so does this.
    // SAFETY: `s` is an exact str (checked above).
    let ascii = unsafe { ffi::PyUnicode_IS_ASCII(s.as_ptr()) } != 0;
    let out = match units(s) {
        Some(PyStringData::Ucs1(b)) => collapse(py, b, ascii),
        Some(PyStringData::Ucs2(w)) => collapse(py, w, false),
        Some(PyStringData::Ucs4(d)) => collapse(py, d, false),
        None => return reference(py, "normalize")?.bind(py).call1((text,)),
    };
    match out {
        Some(r) => r,
        None => Ok(text.clone()), // already normalized: the same immutable value
    }
}

/// Append the UTF-8 of `units`; false on a lone surrogate (the reference raises
/// UnicodeEncodeError there). At most 4 bytes per code point.
#[inline]
fn encode_utf8<T: Copy + Into<u32>>(buf: &mut Writer<u8>, units: &[T]) -> bool {
    let n = units.len();
    let mut i = 0;
    while i < n {
        // ASCII stretch: 8 units at a time.
        while i + 8 <= n && units[i..i + 8].iter().fold(0u32, |a, &c| a | c.into()) < 0x80 {
            for &c in &units[i..i + 8] {
                buf.push(c.into() as u8);
            }
            i += 8;
        }
        if i == n {
            break;
        }
        let c: u32 = units[i].into();
        i += 1;
        if c < 0x80 {
            buf.push(c as u8);
        } else if c < 0x800 {
            buf.push(0xC0 | (c >> 6) as u8);
            buf.push(0x80 | (c & 0x3F) as u8);
        } else if c < 0x10000 {
            if (0xD800..=0xDFFF).contains(&c) {
                return false;
            }
            buf.push(0xE0 | (c >> 12) as u8);
            buf.push(0x80 | ((c >> 6) & 0x3F) as u8);
            buf.push(0x80 | (c & 0x3F) as u8);
        } else {
            buf.push(0xF0 | (c >> 18) as u8);
            buf.push(0x80 | ((c >> 12) & 0x3F) as u8);
            buf.push(0x80 | ((c >> 6) & 0x3F) as u8);
            buf.push(0x80 | (c & 0x3F) as u8);
        }
    }
    true
}

/// Append `" ".join(units.split())` (when `normalize`) or `units`, as UTF-8, in one pass: the
/// runs between changed units (simd::Unit::next_bad) are encoded straight into the key.
fn push_units<T: simd::Unit>(buf: &mut Writer<u8>, units: &[T], normalize: bool) -> bool {
    if !normalize {
        return encode_utf8(buf, units);
    }
    let start = buf.len;
    let (mut i, mut prev_space, mut last_space) = (0usize, true, false);
    while i < units.len() {
        let j = T::next_bad(units, i, prev_space);
        if j > i {
            if !encode_utf8(buf, &units[i..j]) {
                return false;
            }
            last_space = units[j - 1] == T::SPACE;
        }
        if j == units.len() {
            break;
        }
        if buf.len > start && !last_space {
            buf.push(b' ');
            last_space = true;
        }
        prev_space = true;
        i = j + 1;
    }
    if last_space {
        buf.len -= 1; // the trailing separator
    }
    true
}

fn push_str(buf: &mut Writer<u8>, d: &PyStringData<'_>, normalize: bool) -> bool {
    match *d {
        PyStringData::Ucs1(b) => push_units(buf, b, normalize),
        PyStringData::Ucs2(w) => push_units(buf, w, normalize),
        PyStringData::Ucs4(d) => push_units(buf, d, normalize),
    }
}

fn cp_len(d: &PyStringData<'_>) -> usize {
    match *d {
        PyStringData::Ucs1(b) => b.len(),
        PyStringData::Ucs2(w) => w.len(),
        PyStringData::Ucs4(d) => d.len(),
    }
}

fn hash_key(
    text: &Bound<'_, PyAny>,
    src: &Bound<'_, PyAny>,
    tgt: &Bound<'_, PyAny>,
) -> Option<[u8; 64]> {
    let (src, tgt) = (exact_str(src)?, exact_str(tgt)?);
    let (src, tgt) = (units(src)?, units(tgt)?);
    let body = if text.is_none() {
        None
    } else {
        Some(units(exact_str(text)?)?)
    };
    let cap = 4 * (cp_len(&src) + cp_len(&tgt) + body.as_ref().map_or(0, cp_len)) + 2;
    let digest = with_scratch(cap, |buf: &mut Writer<u8>| {
        if !push_str(buf, &src, false) {
            return None;
        }
        buf.push(0x1f);
        if !push_str(buf, &tgt, false) {
            return None;
        }
        buf.push(0x1f);
        if let Some(b) = &body {
            if !push_str(buf, b, true) {
                return None;
            }
        }
        Some(ring::digest::digest(&ring::digest::SHA256, buf.as_slice()))
    })?;
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = [0u8; 64];
    for (i, &b) in digest.as_ref().iter().enumerate() {
        out[2 * i] = HEX[(b >> 4) as usize];
        out[2 * i + 1] = HEX[(b & 15) as usize];
    }
    Some(out)
}

/// True when `obj` (exact str or None) has no `⟦`/`⟧` and, if `need_tag`, no "TAG": then the
/// tagless shortcut applies. None for anything outside the fast path.
fn tagless(obj: &Bound<'_, PyAny>, check_tag: bool) -> Option<bool> {
    fn scan<T: Copy + Into<u32>>(u: &[T], check_tag: bool) -> bool {
        let mut prev2 = 0u32;
        let mut prev1 = 0u32;
        for &c in u {
            let c: u32 = c.into();
            if c == text::OPEN || c == text::CLOSE {
                return false;
            }
            if check_tag && c == 0x47 && prev1 == 0x41 && prev2 == 0x54 {
                return false;
            }
            prev2 = prev1;
            prev1 = c;
        }
        true
    }
    if obj.is_none() {
        return Some(true);
    }
    Some(match units(exact_str(obj)?)? {
        PyStringData::Ucs1(b) => !check_tag || scan(b, true), // U+27E6/7 cannot be stored in 1 byte
        PyStringData::Ucs2(w) => scan(w, check_tag),
        PyStringData::Ucs4(d) => scan(d, check_tag),
    })
}

/// quality_common.tag_damage: the inline-tag damage verdict, "" when clean. None: use the
/// reference (an argument outside the fast path, or a verdict that would quote a surrogate).
fn tag_damage<'py>(
    py: Python<'py>,
    source_text: &Bound<'py, PyAny>,
    target_text: &Bound<'py, PyAny>,
) -> Option<RawResult<'py>> {
    if let (Some(true), Some(true)) = (tagless(source_text, false), tagless(target_text, true)) {
        return Some(Ok(PyString::new(py, "").into_any()));
    }
    let (s, t) = (code_points(source_text)?, code_points(target_text)?);
    tags::tag_damage(&s, &t).map(|v| Ok(PyString::new(py, &v).into_any()))
}

/// segment._legacy_split_py: the built-in splitter (version 1 or 2; no settings, no SRX).
/// None: use the reference (an argument outside the fast path).
fn split_sentences<'py>(
    py: Python<'py>,
    text: &Bound<'py, PyAny>,
    locale: Option<&Bound<'py, PyAny>>,
    version: Option<&Bound<'py, PyAny>>,
) -> Option<RawResult<'py>> {
    // The reference only asks `version == 2`; for an exact int anything else is version 1.
    // Other types (2.0, True, None, numpy ints) go to the reference, whose `==` decides.
    let ver = match version {
        None => 2u8,
        Some(o) if o.is_exact_instance_of::<PyInt>() => {
            if o.extract::<i64>().ok() == Some(2) {
                2
            } else {
                1
            }
        }
        Some(_) => return None,
    };
    if text.is_none() {
        return None; // the reference raises AttributeError on None.strip()
    }
    let t = code_points(text)?;
    let l = match locale {
        Some(l) => code_points(l)?,
        None => Cow::Borrowed(&[][..]),
    };
    let pieces = split::split(&t, &l, ver);
    // SAFETY: a new list of exactly pieces.len() slots, each filled once with a new reference
    // (PyList_SET_ITEM steals it); on failure the partial list is dropped.
    Some(unsafe {
        (|| {
            let list = ffi::PyList_New(pieces.len() as ffi::Py_ssize_t);
            let list = Bound::from_owned_ptr_or_err(py, list)?;
            for (i, (a, b)) in pieces.into_iter().enumerate() {
                let piece = ffi::PyUnicode_Substring(
                    text.as_ptr(),
                    a as ffi::Py_ssize_t,
                    b as ffi::Py_ssize_t,
                );
                if piece.is_null() {
                    return Err(PyErr::fetch(py));
                }
                ffi::PyList_SET_ITEM(list.as_ptr(), i as ffi::Py_ssize_t, piece);
            }
            Ok(list)
        })()
    })
}

// --- Raw CPython entry points ---------------------------------------------------------------
// normalize, hash_text and tag_damage cost well under a microsecond, so PyO3's generic call
// wrapper (about 60 ns on the dev box, against about 25 ns for a builtin such as len) was a large
// share of each call. All four are therefore plain METH_FASTCALL | METH_KEYWORDS functions.
// Any keyword argument or unexpected arity is forwarded unchanged to the Python reference
// (PyObject_Vectorcall), so the reference's own TypeErrors are what a caller sees. A Rust panic
// becomes a RuntimeError, never an abort.

type RawResult<'py> = PyResult<Bound<'py, PyAny>>;

/// Run `f` with the attached interpreter; translate errors and panics to a NULL return.
unsafe fn raw_call<'py>(py: Python<'py>, f: impl FnOnce() -> RawResult<'py>) -> *mut ffi::PyObject {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)) {
        Ok(Ok(obj)) => obj.into_ptr(),
        Ok(Err(e)) => {
            e.restore(py);
            std::ptr::null_mut()
        }
        Err(_) => {
            PyRuntimeError::new_err("locos_native: internal error (Rust panic)").restore(py);
            std::ptr::null_mut()
        }
    }
}

/// Forward the exact call to the registered Python reference.
unsafe fn forward<'py>(
    py: Python<'py>,
    name: &str,
    args: *const *mut ffi::PyObject,
    nargs: ffi::Py_ssize_t,
    kwnames: *mut ffi::PyObject,
) -> RawResult<'py> {
    let f = reference(py, name)?;
    let out = ffi::PyObject_Vectorcall(f.as_ptr(), args, nargs as usize, kwnames);
    Bound::from_owned_ptr_or_err(py, out)
}

unsafe extern "C" fn normalize_raw(
    _slf: *mut ffi::PyObject,
    args: *const *mut ffi::PyObject,
    nargs: ffi::Py_ssize_t,
    kwnames: *mut ffi::PyObject,
) -> *mut ffi::PyObject {
    let py = Python::assume_attached();
    raw_call(py, || {
        if nargs != 1 || !kwnames.is_null() {
            return forward(py, "normalize", args, nargs, kwnames);
        }
        normalize(py, &Bound::from_borrowed_ptr(py, *args))
    })
}

unsafe extern "C" fn hash_text_raw(
    _slf: *mut ffi::PyObject,
    args: *const *mut ffi::PyObject,
    nargs: ffi::Py_ssize_t,
    kwnames: *mut ffi::PyObject,
) -> *mut ffi::PyObject {
    let py = Python::assume_attached();
    raw_call(py, || {
        if nargs != 3 || !kwnames.is_null() {
            return forward(py, "hash_text", args, nargs, kwnames);
        }
        let a = |i: usize| Bound::from_borrowed_ptr(py, *args.add(i));
        match hash_key(&a(0), &a(1), &a(2)) {
            Some(h) => {
                // The 64 ASCII hex digits, written straight into a new compact ASCII str.
                let obj = ffi::PyUnicode_New(64, 0x7F);
                if obj.is_null() {
                    return Err(PyErr::fetch(py));
                }
                std::ptr::copy_nonoverlapping(h.as_ptr(), ffi::PyUnicode_DATA(obj) as *mut u8, 64);
                Ok(Bound::from_owned_ptr(py, obj))
            }
            None => forward(py, "hash_text", args, nargs, kwnames),
        }
    })
}

unsafe extern "C" fn tag_damage_raw(
    _slf: *mut ffi::PyObject,
    args: *const *mut ffi::PyObject,
    nargs: ffi::Py_ssize_t,
    kwnames: *mut ffi::PyObject,
) -> *mut ffi::PyObject {
    let py = Python::assume_attached();
    raw_call(py, || {
        if nargs != 2 || !kwnames.is_null() {
            return forward(py, "tag_damage", args, nargs, kwnames);
        }
        let (s, t) = (
            Bound::from_borrowed_ptr(py, *args),
            Bound::from_borrowed_ptr(py, *args.add(1)),
        );
        match tag_damage(py, &s, &t) {
            Some(v) => v,
            None => forward(py, "tag_damage", args, nargs, kwnames),
        }
    })
}

unsafe extern "C" fn split_sentences_raw(
    _slf: *mut ffi::PyObject,
    args: *const *mut ffi::PyObject,
    nargs: ffi::Py_ssize_t,
    kwnames: *mut ffi::PyObject,
) -> *mut ffi::PyObject {
    let py = Python::assume_attached();
    raw_call(py, || {
        // Fast path: (text[, locale][, version=...]); anything else is the reference's call.
        let mut version = None;
        if !kwnames.is_null() {
            let names = Bound::from_borrowed_ptr(py, kwnames);
            let n = ffi::PyTuple_GET_SIZE(kwnames);
            let only_version = n == 1
                && names
                    .get_item(0)
                    .ok()
                    .and_then(|k| exact_str(&k).map(|s| s.to_str().ok() == Some("version")))
                    == Some(true);
            if !only_version {
                return forward(py, "split_sentences", args, nargs, kwnames);
            }
            version = Some(Bound::from_borrowed_ptr(py, *args.add(nargs as usize)));
        }
        if !(1..=2).contains(&nargs) {
            return forward(py, "split_sentences", args, nargs, kwnames);
        }
        let text = Bound::from_borrowed_ptr(py, *args);
        let locale = (nargs == 2).then(|| Bound::from_borrowed_ptr(py, *args.add(1)));
        match split_sentences(py, &text, locale.as_ref(), version.as_ref()) {
            Some(v) => v,
            None => forward(py, "split_sentences", args, nargs, kwnames),
        }
    })
}

/// Add a raw METH_FASTCALL | METH_KEYWORDS function to the module.
fn add_raw(
    m: &Bound<'_, PyModule>,
    name: &'static std::ffi::CStr,
    doc: &'static std::ffi::CStr,
    f: ffi::PyCFunctionFastWithKeywords,
) -> PyResult<()> {
    let def = Box::leak(Box::new(ffi::PyMethodDef {
        ml_name: name.as_ptr(),
        ml_meth: ffi::PyMethodDefPointer {
            PyCFunctionFastWithKeywords: f,
        },
        ml_flags: ffi::METH_FASTCALL | ffi::METH_KEYWORDS,
        ml_doc: doc.as_ptr(),
    }));
    // SAFETY: `def` is leaked, so it outlives the function object, as CPython requires.
    let func = unsafe {
        Bound::from_owned_ptr_or_err(
            m.py(),
            ffi::PyCFunction_NewEx(def, std::ptr::null_mut(), m.name()?.as_ptr()),
        )?
    };
    m.add(name.to_str().unwrap_or("?"), func)
}

#[pymodule]
fn locos_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("UNIDATA_VERSION", tables::UNIDATA_VERSION)?;
    m.add("STAMPS", stamps::STAMPS.to_vec())?;
    m.add_function(wrap_pyfunction!(_set_reference, m)?)?;
    add_raw(
        m,
        c"normalize",
        c"tm_common.normalize, natively.",
        normalize_raw,
    )?;
    add_raw(
        m,
        c"hash_text",
        c"tm_common.hash_text, natively.",
        hash_text_raw,
    )?;
    add_raw(
        m,
        c"tag_damage",
        c"quality_common.tag_damage, natively.",
        tag_damage_raw,
    )?;
    add_raw(
        m,
        c"split_sentences",
        c"segment._legacy_split_py, natively.",
        split_sentences_raw,
    )?;
    Ok(())
}
