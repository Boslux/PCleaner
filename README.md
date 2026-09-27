# PCleaner

**Windows için hafif, hızlı ve anlaşılır disk temizliği.** Geçici dosyaları ve seçtiğin önbellekleri tarayıp önizle; kullanımdaki dosyalar atlanırken temizliği terminalden veya sade masaüstü arayüzünden yönet.

PCleaner yerel çalışır. Python standart kütüphanesi ve Tkinter dışında çalışma zamanı bağımlılığı yoktur.

## Öne çıkanlar

- Hem kullanıcıya ait `%TEMP%` klasörünü hem de Windows klasöründeki `Temp` alanını tarar.
- Tarama sonuçlarını temizlemeden önce gösterir; istemediğin dosyaları seçimden çıkarabilirsin.
- Başka bir uygulama tarafından kullanılan veya erişilemeyen dosyaları atlar, kalan temizliğe devam eder ve raporlar.
- Son Kullanılanlar kısayollarını kaldırabilir; hedef dosyalara dokunmaz.
- İsteğe bağlı tarayıcı önbelleği ve Windows küçük resim önbelleği temizliği sunar.
- Terminal komutlarıyla tarama, önizleme, yaş filtresi, rapor ve iptal desteği sağlar.
- Temizlik öncesinde hangi alanların seçildiğini ve ne kadar yer açıldığını gösterir.

## Hızlı başlangıç

PowerShell veya Terminal'i proje klasöründe aç:

```powershell
python pcleaner.py
```

Windows'ta `PCleaner.bat` dosyasına çift tıklayarak da başlatabilirsin.

## Terminalden kullan

Önce yedi günden eski uygun dosyaları tara:

```powershell
python pcleaner.py scan --days 7
```

Silmeden deneme yap:

```powershell
python pcleaner.py clean user-temp windows-temp browser-cache --days 7 --dry-run
```

Onay vererek temizle:

```powershell
python pcleaner.py clean user-temp windows-temp recent
```

`clean` komutu `--yes` verilmedikçe terminalde onay ister. `--dry-run` hiçbir dosyayı silmez. Desteklenen yaş filtreleri 1, 3, 7, 14 ve 30 gündür.

### Temizlenebilen alanlar

| Komut | Alan |
| --- | --- |
| `user-temp` | Kullanıcının `%TEMP%` klasöründeki uygun dosyalar |
| `windows-temp` | Windows klasöründeki `Temp` içindeki uygun dosyalar |
| `recent` | Son Kullanılanlar klasöründeki `.lnk` kısayolları; hedef dosyalar korunur |
| `browser-cache` | Chrome, Edge ve Firefox profillerindeki cache dosyaları |
| `thumbnail-cache` | Windows Explorer küçük resim ve simge önbelleği veritabanları |
| `recycle` | Geri Dönüşüm Kutusu; içindeki öğeler kalıcı olarak silinir |

`temp`, `user-temp` için eski bir komut takma adıdır. Tarayıcı geçmişi, çerezler ve oturum dosyaları tarayıcı temizliğine dahil değildir. Tarayıcı ve küçük resim önbelleği varsayılan olarak seçili değildir. Kilitli veya erişilemeyen öğeler atlanır; diğer dosyalar işlenmeye devam eder.

## Taşınabilir EXE oluştur

EXE'yi derlemek için PyInstaller gerekir; oluşturulan uygulamayı çalıştırmak için bilgisayarda Python kurulması gerekmez.

```powershell
python -m pip install -r requirements-build.txt
.\build_exe.ps1
```

Çıktılar `release\PCleaner\PCleaner.exe` ve `release\PCleaner-portable.zip` konumlarına yazılır. EXE'yi `_internal` klasörüyle birlikte taşı veya ZIP içeriğini kullan.

## Güvenli kullanım

- İlk kullanımda arayüzde dosya önizlemesini incele veya terminalde `--dry-run` kullan.
- Temizleme yalnızca seçili alanları kapsar; uygulamanın kendisi sistem ayarlarını değiştirmez.
- Geri Dönüşüm Kutusu temizliği kalıcıdır.

## Lisans

Bu depo henüz bir açık kaynak lisansı tanımlamıyor. Yeniden kullanım ve dağıtım koşullarını belirlemek için bir lisans eklenebilir.
