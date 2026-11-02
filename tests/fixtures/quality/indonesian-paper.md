# **EKSTRAKSI INFORMASI NILAI GIZI PADA LABEL KEMASAN MENGGUNAKAN CONFUSION-AWARE WEIGHTED LEVENSHTEIN DISTANCE DAN DIRECTION-AWARE SPATIAL ALIGNMENT**

## Abstract

Vol. 10 No. 5, Oktober 2026

**JATI** _(Jurnal Mahasiswa Teknik Informatika)_ 

Vol. 10 No. 5, Oktober 2026 

# **EKSTRAKSI INFORMASI NILAI GIZI PADA LABEL KEMASAN MENGGUNAKAN CONFUSION-AWARE WEIGHTED LEVENSHTEIN DISTANCE DAN DIRECTION-AWARE SPATIAL ALIGNMENT** 

## **Ari Fuzzaman, Tito Pinandita, Elindra Ambar Pambudi, Maulida Ayu Fitriani** 

Teknik Informatika, Universitas Muhammadiyah Purwokerto Jl. KH. Ahmad Dahlan, Dusun III, Dukuhwaluh, Kec. Kembaran, Kabupaten Banyumas, Jawa Tengah 53182, Indonesia 

_zamanari01@gmail.com_ 

## **ABSTRAK** 

Kesadaran masyarakat terhadap kandungan gizi produk makanan semakin meningkat, mendorong kebutuhan sistem ekstraksi informasi nilai gizi secara otomatis berbasis perangkat _mobile_ . Permasalahan utama dalam otomatisasi pembacaan label nutrisi terletak pada kesalahan asosiasi spasial antara label dan nilai numerik, serta kesalahan substitusi karakter pada hasil OCR akibat kemiripan visual antar karakter. Penelitian ini bertujuan membangun ekstraksi informasi nilai gizi yang mampu membaca delapan nutrisi utama secara _real-time_ dari citra kemasan makanan. Metode yang dikembangkan mengintegrasikan _Confusion-Aware Weighted Levenshtein Distance_ (WLD) untuk koreksi teks OCR berbasis matriks konfusi karakter, dan _Direction-Aware Spatial Alignment_ untuk penyelarasan nilai numerik berbasis _bounding box_ , diimplementasikan dalam arsitektur MVVM dengan mekanisme _human-in-the-loop_ dan rekomendasi _Generative_ AI. Pengujian terhadap 30 sampel kemasan (240 _field_ ) menunjukkan rata-rata _Precision_ 98,69%, _Recall_ 82,07%, _F1-score_ 89,61%, dan _Accuracy_ 85,42%, dengan rata-rata MAE 4,296. Sistem terbukti andal pada kemasan datar, meskipun label semi-terstruktur dan kemasan melengkung masih menjadi tantangan yang perlu diatasi pada penelitian selanjutnya. 

## **_Kata kunci :_** _OCR, Nilai Gizi, Weighted Levenshtein Distance, Spatial Alignment, Human-in-the-loop_ 

## **1. PENDAHULUAN** 

Informasi nilai gizi pada kemasan makanan merupakan instrumen penting dalam pengambilan keputusan konsumsi, khususnya dalam konteks peningkatan kesadaran masyarakat terhadap pola makan sehat. Digitalisasi informasi melalui perangkat _mobile_ mendorong pengembangan sistem otomatis untuk membaca dan menginterpretasikan label nutrisi secara _real-time_ . Teknologi _Optical Character Recognition_ (OCR) telah banyak digunakan untuk mengekstraksi teks dari citra kemasan makanan [1], termasuk pada penelitian yang memanfaatkan kombinasi deteksi objek dan OCR untuk mengidentifikasi kandungan nutrisi tertentu seperti natrium [2]. 

Namun demikian, label nilai gizi memiliki karakteristik semi-terstruktur menyerupai tabel tanpa garis eksplisit, sehingga hubungan antara label dan nilai numerik tidak selalu dapat diidentifikasi secara langsung melalui OCR mentah [3]. Permasalahan utama dalam ekstraksi label gizi tidak hanya terletak pada kesalahan pengenalan karakter, tetapi juga pada kesalahan asosiasi spasial antara label dan nilai. OCR sering menghasilkan substitusi karakter akibat kemiripan visual, seperti _g_ terbaca sebagai _9_ , _l_ sebagai _1_ , atau _o_ sebagai _0_ , yang berdampak langsung pada hilangnya informasi nutrisi tertentu [4], [5]. Selain itu, tanpa mekanisme penyelarasan berbasis koordinat, sistem cenderung mengambil nilai dari baris yang tidak sesuai atau dari bagian _footer_ seperti kebutuhan energi harian (misalnya 2150 kkal). Pendekatan berbasis _deep learning_ untuk analisis struktur 

dokumen memang menunjukkan performa tinggi, namun memerlukan _dataset_ pelatihan yang besar dan sumber daya komputasi signifikan [6]. 

Penelitian ini bertujuan membangun ekstraksi informasi nilai gizi yang mampu membaca delapan nutrisi utama secara akurat dan _real-time_ dari citra kemasan makanan, dengan menggabungkan koreksi karakter berbasis konfusi visual dan penyelarasan spasial berbasis arah secara terintegrasi dalam satu _pipeline_ ringan tanpa pelatihan model tambahan. 

Oleh karena itu, penelitian ini mengusulkan pendekatan hibrida yang mengintegrasikan _ConfusionAware Weighted Levenshtein Distance_ untuk meningkatkan toleransi kesalahan karakter OCR, serta _Direction-Aware Spatial Alignment_ berbasis _bounding box_ untuk menjaga konsistensi relasi label–nilai pada struktur semi-terstruktur. Pendekatan ini dirancang bersifat deterministik, ringan, dan tidak memerlukan pelatihan model tambahan. Sistem diimplementasikan menggunakan arsitektur MVVM pada Android dan dilengkapi mekanisme _human-in-the-loop_ untuk validasi manual sebelum integrasi dengan modul rekomendasi berbasis _Generative_ AI. 

## **2. TINJAUAN PUSTAKA** 

## **2.1. Penelitian Terdahulu** 

Sejumlah penelitian sebelumnya telah berupaya mengembangkan ekstraksi informasi dan teks secara otomatis. Dalam konteks analisis kemasan, E.Kina [1] mengembang