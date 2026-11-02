# **KLASIFIKASI JENIS SAMPAH RUMAH TANGGA MENGGUNAKAN JARINGAN SARAF KONVOLUSI RINGAN PADA PERANGKAT BERDAYA RENDAH**

## Abstract

Vol. 4 No. 2, Agustus 2026

**JCI** _(Jurnal Contoh Informatika)_ 

Vol. 4 No. 2, Agustus 2026 

# **KLASIFIKASI JENIS SAMPAH RUMAH TANGGA MENGGUNAKAN JARINGAN SARAF KONVOLUSI RINGAN PADA PERANGKAT BERDAYA RENDAH** 

## **Sekar Wulandaru, Damar Prasetyono, Lintang Ayuningtyas** 

Program Studi Informatika, Universitas Contoh Nusantara, Jl. Contoh Raya No. 12, Kota Contoh 12345 

_sekar.wulandaru@example.org_ 

## **ABSTRAK** 

Pemilahan sampah rumah tangga sejak dari sumbernya masih jarang dilakukan karena memerlukan waktu dan pengetahuan tentang jenis sampah. Penelitian ini merancang sistem klasifikasi otomatis berbasis citra yang dapat dijalankan pada perangkat berdaya rendah, sehingga dapat dipasang langsung pada tempat sampah. Model yang digunakan adalah jaringan saraf konvolusi ringan dengan jumlah parameter kurang dari satu juta, dilatih menggunakan 6.400 citra yang terbagi ke dalam lima kelas: organik, plastik, kertas, logam, dan kaca. Untuk mengatasi jumlah data yang terbatas, dilakukan augmentasi berupa rotasi, perubahan kecerahan, dan pemotongan acak. Hasil pengujian menunjukkan akurasi sebesar 91,3% pada data uji, dengan waktu inferensi rata-rata 84 milidetik per citra pada perangkat uji. Kesalahan klasifikasi paling banyak terjadi antara kelas plastik dan kaca, terutama pada benda bening. Sistem ini diharapkan dapat membantu warga memilah sampah dengan lebih mudah dan mengurangi beban tempat pemrosesan akhir. 

**Kata kunci:** _klasifikasi citra, jaringan saraf konvolusi, pemilahan sampah, perangkat berdaya rendah_ 

## **1. PENDAHULUAN** 

Jumlah sampah rumah tangga di kota-kota besar terus meningkat setiap tahun, sementara kapasitas tempat pemrosesan akhir semakin terbatas. Salah satu cara untuk mengurangi beban tersebut adalah memilah sampah sejak dari rumah, sehingga sampah yang masih dapat didaur ulang tidak tercampur dengan sampah organik. Namun, kebiasaan memilah sampah belum banyak dijalankan oleh warga, antara lain karena kurangnya pengetahuan tentang jenis sampah dan anggapan bahwa pemilahan memerlukan banyak waktu. 

Kemajuan pengolahan citra digital membuka peluang untuk membantu proses pemilahan secara otomatis. Berbagai penelitian sebelumnya telah menerapkan jaringan saraf konvolusi untuk mengenali jenis sampah dengan akurasi yang tinggi, tetapi sebagian besar model tersebut berukuran besar dan memerlukan komputer dengan kartu grafis. Kebutuhan daya dan biaya perangkat seperti itu menjadi kendala apabila sistem akan dipasang di banyak titik. 

Oleh karena itu, penelitian ini berfokus pada model yang cukup kecil untuk dijalankan pada papan mikrokomputer berdaya rendah tanpa kehilangan banyak akurasi. Kontribusi penelitian ini adalah rancangan arsitektur ringan, kumpulan data citra sampah lokal yang dikumpulkan sendiri, serta pengujian waktu inferensi pada perangkat sebenarnya. 

## **2. METODE PENELITIAN** 

Tahapan penelitian terdiri atas pengumpulan data, prapemrosesan, perancangan model, pelatihan, dan pengujian. Citra dikumpulkan menggunakan kamera ponsel dengan latar belakang yang beragam, kemudian diubah ukurannya menjadi 128 × 128 piksel dan dinormalisasi. 
