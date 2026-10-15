# Everquill — provider API resmi Meta

Everquill adalah harness coding-agent terminal independen, bukan produk resmi
Meta. Distribusi publik ini hanya mendukung provider Meta. Context proyek,
proposal/diff, approval, penulisan aman, validasi, session dan TUI tetap berada
di bawah kendali lokal. Model tidak memberikan izin kepada dirinya sendiri
untuk menjalankan tool atau mengubah file.

Runtime Python memakai standard library dan memerlukan Python 3.11 atau lebih
baru. Node.js 22+ opsional untuk TUI. Mode classic/one-shot tidak memerlukan
Node. Wheel dan source archive publik tidak memuat transport Muse historis,
routes/descriptors hasil ekstraksi, autentikasi browser, maupun laporan internal.
Cookie browser dan akses layanan tidak resmi tidak didukung.

## Instalasi dan penggunaan

Setelah meninjau artifact yang disetujui Owner, instal wheel lokal menggunakan
pip dalam virtual environment. Publikasi dan release registry masih memerlukan
approval terpisah; panduan ini tidak mengklaim package sudah tersedia di registry.

```sh
python -m pip install ./everquill-0.1.0-py3-none-any.whl
everquill --help
everquill --version
everquill --provider meta --session "Explain this project"
everquill --provider meta --continue "Review the previous answer"
everquill --no-tui "Explain sample.py"
everquill --proposal "Propose a small correction"
```

Permintaan live memerlukan `MODEL_API_KEY` yang disediakan Owner melalui
environment proses. Jangan simpan credential dalam proyek atau laporan.
`EVERQUILL_PROVIDER=meta` didukung tanpa fallback. Jika key tidak tersedia,
permintaan yang disetujui menghasilkan pesan error dan exit nonzero. Live Meta
API verification tetap DEFERRED. Fixture offline membuktikan kontrak, bukan
ketersediaan layanan, entitlement akun, maupun akses model live.

`everquill gateway ...` dan `--provider muse` tidak didukung dalam artifact
publik dan menghasilkan error nonzero yang jelas. Source historis tetap lokal,
di luar package, menunggu review provenance/distribution. Exclusion bukan
kesimpulan hukum mengenai material tersebut.

## Consent, tool dan session

Pengiriman context memerlukan `y` atau `yes` lowercase secara eksplisit.
Tool yang dipanggil model hanya `read_file` read-only dengan batas dan screening.
Shell/write tool tidak diaktifkan otomatis. Pengiriman hasil tool memerlukan
consent baru. Proposal MODIFY/CREATE/DELETE tetap melalui diff, validasi dan
approval. DELETE juga memerlukan konfirmasi target; TUI menolak DELETE karena
tidak dapat mengumpulkan konfirmasi tersebut.

Jalankan dari direktori proyek. State disimpan di
`~/.config/everquill/sessions.json`, termasuk path proyek, response/call ID dan
kemungkinan teks hasil tool. Jangan commit/share state. Atomic replacement
mencegah JSON parsial, bukan enkripsi, autentikasi state atau lock antarproses.

Recovery melalui `--continue`:

- `prepared`: approval eksekusi baru sebelum membaca, lalu approval pengiriman.
- `results_ready`: kirim hasil tersimpan setelah consent, tanpa membaca ulang.
- `executing` / `continuation_started`: outcome ambigu; STOP untuk manual review,
  tanpa blind execute/resend dan tanpa klaim exactly-once.

Recovery hanya menyelesaikan turn read-only tersimpan. Prompt baru yang
ditambahkan tidak dikirim dalam invocation recovery; gunakan request berikutnya.
Metadata rusak/tidak lengkap fail-safe; state text-only historis tetap didukung.
Journal yang seluruhnya dihapus tidak dapat dibedakan dari absence legacy.

## Exit codes dan batas keamanan

Kode one-shot/recovery: `0` answer/proposal selesai atau valid no changes; `1`
pekerjaan gagal/tidak lengkap, termasuk provider, persistence, recovery ambigu
atau proposal ditolak; `2` argumen/command unsupported atau lookup session error;
`130` cancellation atau consent context/tool ditolak, termasuk EOF/input tidak
dikenal. Lifetime interactive adalah kontrak terpisah.

Task 10 containment tetap DEFERRED. Eksekusi test yang disetujui memiliki LIMITED
isolation, bukan OS sandbox. Arbitrary descendants, file eksternal dan permission
akun belum sepenuhnya terisolasi. Cancellation/timeout lokal tidak membuktikan
remote work atau semua descendants telah berhenti. Progressive streaming dan
strict cancellation bounds tetap DEFERRED; tidak ada klaim live verification
atau jaminan keamanan universal.

## License dan batas source

Copyright/permission notice MIT upstream dipertahankan dalam `LICENSE`.
Everquill berasal dari extension nikships/muse-cli. Helper/adaptasi lokal yang
dipertahankan tidak diklaim sebagai sole-authored atau clean-room. Rationale
boundary ada di [BOUNDARY.md](BOUNDARY.md); [package-manifest.json](package-manifest.json)
adalah seleksi source eksplisit, bukan izin publikasi. Record D1/D2 historis
 tetap private dan unchanged. Review attribution serta izin publikasi akhir
tetap menjadi keputusan Owner. Jika terjemahan berbeda, [English](README.md)
adalah teks canonical.
