// ============================================================
//  Smart Form Filler Core
//  负责：共用工具（随机前缀、填表数据构建、CSV 记录解析）
// ============================================================

const SmartFormFillerCore = (() => {
  // ----- 美国免税州城市/邮编对照（与「信息填充助手」一致） -----
  const TAX_FREE_LOCATIONS = [
    { city: "Wilmington", state: "Delaware", zips: ["19801","19802","19803","19804","19805","19806","19807","19808","19809","19810"] },
    { city: "Dover", state: "Delaware", zips: ["19901","19902","19904"] },
    { city: "Newark", state: "Delaware", zips: ["19702","19711","19713","19716"] },
    { city: "Middletown", state: "Delaware", zips: ["19709"] },
    { city: "Bear", state: "Delaware", zips: ["19701"] },
    { city: "Smyrna", state: "Delaware", zips: ["19977"] },
    { city: "Milford", state: "Delaware", zips: ["19963"] },
    { city: "Seaford", state: "Delaware", zips: ["19973"] },
    { city: "Georgetown", state: "Delaware", zips: ["19947"] },
    { city: "Lewes", state: "Delaware", zips: ["19958"] },
    { city: "Rehoboth Beach", state: "Delaware", zips: ["19971"] },
    { city: "Camden", state: "Delaware", zips: ["19934"] },
    { city: "Harrington", state: "Delaware", zips: ["19952"] },
    { city: "New Castle", state: "Delaware", zips: ["19720"] },
    { city: "Claymont", state: "Delaware", zips: ["19703"] },
    { city: "Billings", state: "Montana", zips: ["59101","59102","59105","59106"] },
    { city: "Missoula", state: "Montana", zips: ["59801","59802","59803","59804"] },
    { city: "Great Falls", state: "Montana", zips: ["59401","59404","59405"] },
    { city: "Bozeman", state: "Montana", zips: ["59715","59718"] },
    { city: "Helena", state: "Montana", zips: ["59601","59602"] },
    { city: "Butte", state: "Montana", zips: ["59701"] },
    { city: "Kalispell", state: "Montana", zips: ["59901"] },
    { city: "Whitefish", state: "Montana", zips: ["59937"] },
    { city: "Havre", state: "Montana", zips: ["59501"] },
    { city: "Anaconda", state: "Montana", zips: ["59711"] },
    { city: "Miles City", state: "Montana", zips: ["59301"] },
    { city: "Livingston", state: "Montana", zips: ["59047"] },
    { city: "Laurel", state: "Montana", zips: ["59044"] },
    { city: "Sidney", state: "Montana", zips: ["59270"] },
    { city: "Lewistown", state: "Montana", zips: ["59457"] },
    { city: "Manchester", state: "New Hampshire", zips: ["03101","03102","03103","03104","03109"] },
    { city: "Nashua", state: "New Hampshire", zips: ["03060","03062","03063","03064"] },
    { city: "Concord", state: "New Hampshire", zips: ["03301","03303"] },
    { city: "Portsmouth", state: "New Hampshire", zips: ["03801","03802"] },
    { city: "Rochester", state: "New Hampshire", zips: ["03867","03868"] },
    { city: "Salem", state: "New Hampshire", zips: ["03079"] },
    { city: "Dover", state: "New Hampshire", zips: ["03820"] },
    { city: "Keene", state: "New Hampshire", zips: ["03431"] },
    { city: "Laconia", state: "New Hampshire", zips: ["03246"] },
    { city: "Lebanon", state: "New Hampshire", zips: ["03766"] },
    { city: "Claremont", state: "New Hampshire", zips: ["03743"] },
    { city: "Derry", state: "New Hampshire", zips: ["03038"] },
    { city: "Durham", state: "New Hampshire", zips: ["03824"] },
    { city: "Hanover", state: "New Hampshire", zips: ["03755"] },
    { city: "Exeter", state: "New Hampshire", zips: ["03833"] },
    { city: "Portland", state: "Oregon", zips: ["97201","97202","97203","97204","97205","97206","97209","97210","97211","97212","97213","97214","97215","97217","97219","97220","97221","97222","97223","97224","97225","97227","97229","97230","97231","97232","97233","97236"] },
    { city: "Salem", state: "Oregon", zips: ["97301","97302","97303","97304","97305","97306","97317"] },
    { city: "Eugene", state: "Oregon", zips: ["97401","97402","97403","97404","97405"] },
    { city: "Bend", state: "Oregon", zips: ["97701","97702","97703"] },
    { city: "Medford", state: "Oregon", zips: ["97501","97504"] },
    { city: "Corvallis", state: "Oregon", zips: ["97330","97331","97333"] },
    { city: "Hillsboro", state: "Oregon", zips: ["97123","97124"] },
    { city: "Beaverton", state: "Oregon", zips: ["97005","97006","97007","97008"] },
    { city: "Tigard", state: "Oregon", zips: ["97223","97224"] },
    { city: "Lake Oswego", state: "Oregon", zips: ["97034","97035"] },
    { city: "Gresham", state: "Oregon", zips: ["97030","97080"] },
    { city: "Albany", state: "Oregon", zips: ["97321","97322"] },
    { city: "Ashland", state: "Oregon", zips: ["97520"] },
    { city: "Grants Pass", state: "Oregon", zips: ["97526","97527"] },
    { city: "Redmond", state: "Oregon", zips: ["97756"] },
    { city: "Anchorage", state: "Alaska", zips: ["99501","99502","99503","99504","99507","99508","99515","99516","99517","99518"] },
    { city: "Fairbanks", state: "Alaska", zips: ["99701","99709","99712"] },
    { city: "Juneau", state: "Alaska", zips: ["99801","99802"] },
    { city: "Wasilla", state: "Alaska", zips: ["99654","99687"] },
    { city: "Sitka", state: "Alaska", zips: ["99835"] },
    { city: "Ketchikan", state: "Alaska", zips: ["99901"] },
    { city: "Kenai", state: "Alaska", zips: ["99611"] },
    { city: "Kodiak", state: "Alaska", zips: ["99615"] },
    { city: "Palmer", state: "Alaska", zips: ["99645"] },
    { city: "Soldotna", state: "Alaska", zips: ["99669"] },
    { city: "Homer", state: "Alaska", zips: ["99603"] },
    { city: "Valdez", state: "Alaska", zips: ["99686"] },
    { city: "Seward", state: "Alaska", zips: ["99664"] },
    { city: "North Pole", state: "Alaska", zips: ["99705"] },
    { city: "Eagle River", state: "Alaska", zips: ["99577"] }
  ];

  const STREET_NAMES = [
    "Main St","Oak St","Maple Ave","Cedar Ln","Pine St","Elm St",
    "Washington St","Park Ave","Lake Dr","Hill Rd","Forest Ave",
    "Church St","Spring St","High St","Mill Rd","River Rd",
    "Prospect St","Center St","School St","Union St","Water St",
    "Market St","Court St","Bridge St","Broad St","Green St",
    "State St","Pleasant St","Franklin St","Jefferson Ave",
    "Lincoln Ave","Madison Ave","Jackson St","Adams St","Monroe St",
    "Highland Ave","Sunset Dr","Meadow Ln","Valley Rd","Ridge Rd",
    "Lakeview Dr","Hillside Ave","Woodland Dr","Fairview Ave",
    "Chestnut St","Walnut St","Birch St","Willow Ln","Spruce St",
    "Dogwood Ln","Magnolia Dr","Poplar St","Hickory Ln","Sycamore St",
    "Cherry Ln","Laurel St","Ivy Ln","Holly Dr","Juniper St",
    "Aspen Ct","Beech St","Cypress Ave","Hemlock Dr","Redwood Dr",
    "Colonial Dr","Heritage Ln","Liberty St","Independence Ave",
    "Patriot Way","Eagle Dr","Falcon Rd","Hawk Ln","Cardinal Dr",
    "Oriole Way","Robin Ln","Sparrow Dr","Dove Ct","Finch St",
    "Summit Ave","Crest Dr","Vista Ln","Terrace Dr","Bluff Rd",
    "Canyon Rd","Creek Dr","Brook Ln","Pond Rd","Harbor Dr",
    "Bay St","Shore Dr","Coastal Way","Ocean Ave","Anchor Ln",
    "Beacon St","Lighthouse Rd","Marina Dr","Wharf St","Pier Ave",
    "Country Club Rd","Fairway Dr","Golf Course Rd","Clubhouse Ln",
    "Orchard Rd","Vineyard Dr","Garden St","Rose Ln","Daisy Dr",
    "Sunflower Ln","Violet St","Lily Ct","Tulip Dr","Peony Ln",
    "Industrial Blvd","Commerce Dr","Enterprise Way","Business Park Dr",
    "Technology Dr","Innovation Way","Corporate Blvd","Executive Dr",
    "Northfield Rd","Southgate Dr","Eastwood Ave","Westbrook Ln",
    "Northview Dr","Southwind Dr","Eastside Ave","Westfield Rd",
    "Cambridge Dr","Oxford Rd","Windsor Ln","Canterbury Dr",
    "Buckingham Dr","Westminster Ave","Hampton Rd","Kensington Dr",
    "Victoria Ln","Wellington Ave","Stratford Rd","Coventry Ln",
    "Sherwood Dr","Nottingham Rd","Bristol Ave","Devon Ln",
    "Cornwall Dr","Essex Rd","Suffolk Ln","Norfolk Ave",
    "Briarwood Dr","Stonegate Rd","Ironwood Ln","Timberline Dr",
    "Creekside Dr","Lakewood Ave","Pinecrest Rd","Oakmont Dr",
    "Cedarwood Ln","Maplewood Ave","Elmwood Dr","Birchwood Ln",
    "Willowbrook Dr","Foxwood Ln","Deerfield Rd","Quail Run",
    "Pheasant Ln","Partridge Dr","Grouse Rd","Mallard Dr",
    "Heron Way","Crane Ln","Pelican Dr","Sandpiper Rd",
    "Kingfisher Ln","Osprey Dr","Raven Rd","Wren Ct"
  ];

  const STREET_PREFIXES = ["N","S","E","W","NE","NW","SE","SW"];

  const FIRST_NAMES = [
    "James","Robert","John","Michael","David","William","Richard","Joseph","Thomas","Christopher",
    "Charles","Daniel","Matthew","Anthony","Mark","Donald","Steven","Andrew","Paul","Joshua",
    "Kenneth","Kevin","Brian","George","Timothy","Ronald","Edward","Jason","Jeffrey","Ryan",
    "Jacob","Gary","Nicholas","Eric","Jonathan","Stephen","Larry","Justin","Scott","Brandon",
    "Benjamin","Samuel","Raymond","Gregory","Frank","Alexander","Patrick","Jack","Dennis","Jerry",
    "Tyler","Aaron","Jose","Nathan","Henry","Peter","Douglas","Adam","Zachary","Walter",
    "Mary","Patricia","Jennifer","Linda","Barbara","Elizabeth","Susan","Jessica","Sarah","Karen",
    "Lisa","Nancy","Betty","Margaret","Sandra","Ashley","Dorothy","Kimberly","Emily","Donna",
    "Michelle","Carol","Amanda","Melissa","Deborah","Stephanie","Rebecca","Sharon","Laura","Cynthia",
    "Kathleen","Amy","Angela","Shirley","Brenda","Emma","Anna","Pamela","Nicole","Samantha",
    "Katherine","Christine","Helen","Debra","Rachel","Carolyn","Janet","Catherine","Maria","Heather",
    "Diane","Olivia","Julie","Joyce","Virginia","Victoria","Kelly","Lauren","Christina","Joan"
  ];

  const LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez",
    "Hernandez","Lopez","Gonzalez","Wilson","Anderson","Thomas","Taylor","Moore","Jackson","Martin",
    "Lee","Perez","Thompson","White","Harris","Sanchez","Clark","Ramirez","Lewis","Robinson",
    "Walker","Young","Allen","King","Wright","Scott","Torres","Nguyen","Hill","Flores",
    "Green","Adams","Nelson","Baker","Hall","Rivera","Campbell","Mitchell","Carter","Roberts",
    "Gomez","Phillips","Evans","Turner","Diaz","Parker","Cruz","Edwards","Collins","Reyes",
    "Stewart","Morris","Morales","Murphy","Cook","Rogers","Gutierrez","Ortiz","Morgan","Cooper",
    "Peterson","Bailey","Reed","Kelly","Howard","Ramos","Kim","Cox","Ward","Richardson",
    "Watson","Brooks","Chavez","Wood","James","Bennett","Gray","Mendoza","Ruiz","Hughes",
    "Price","Alvarez","Castillo","Sanders","Patel","Myers","Long","Ross","Foster","Jimenez"
  ];

  function _randomPick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function _randomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

  const UK_LOCATIONS = [
    { city: 'London', state: 'England', zips: ['SW1A 1AA','W1A 1AA','EC1A 1BB','WC2N 5DU'] },
    { city: 'Manchester', state: 'England', zips: ['M1 1AE','M2 5DB','M3 4EN'] },
    { city: 'Birmingham', state: 'England', zips: ['B1 1AA','B2 4QA','B3 1JP'] },
    { city: 'Edinburgh', state: 'Scotland', zips: ['EH1 1BB','EH2 2BY','EH3 9DR'] },
    { city: 'Cardiff', state: 'Wales', zips: ['CF10 1AA','CF10 2HE','CF11 9LJ'] },
    { city: 'Belfast', state: 'Northern Ireland', zips: ['BT1 1AA','BT2 7BA','BT7 1NN'] }
  ];
  const UK_STREETS = ['High Street','Station Road','Church Lane','Victoria Road','Green Lane','King Street','Queen Street','Park Road','Mill Lane','London Road'];
  const UK_FIRST_NAMES = ['Oliver','George','Harry','Jack','Noah','Charlie','Thomas','William','James','Amelia','Olivia','Isla','Emily','Ava','Sophie','Grace','Lily','Ella'];
  const UK_LAST_NAMES = ['Smith','Jones','Taylor','Brown','Williams','Wilson','Johnson','Davies','Patel','Robinson','Wright','Thompson','Evans','Walker'];

  // South/Southeast Asia profile fixtures.  Postal codes are paired with their
  // cities so generated addresses stay internally consistent.
  const IN_LOCATIONS = [
    { city: 'New Delhi', state: 'Delhi', zips: ['110001','110016','110019','110048'] },
    { city: 'Mumbai', state: 'Maharashtra', zips: ['400001','400050','400053','400076'] },
    { city: 'Bengaluru', state: 'Karnataka', zips: ['560001','560038','560066','560102'] },
    { city: 'Hyderabad', state: 'Telangana', zips: ['500001','500034','500081','500084'] },
    { city: 'Chennai', state: 'Tamil Nadu', zips: ['600001','600028','600040','600096'] },
    { city: 'Kolkata', state: 'West Bengal', zips: ['700001','700019','700029','700091'] },
    { city: 'Pune', state: 'Maharashtra', zips: ['411001','411014','411038','411045'] }
  ];
  const IN_STREETS = ['Mahatma Gandhi Road','Park Street','Link Road','Residency Road','Lake View Road','Station Road','Nehru Road','Temple Road','Market Road','Green Park'];
  const IN_FIRST_NAMES = ['Aarav','Arjun','Rohan','Vikram','Aditya','Kabir','Ananya','Diya','Isha','Kavya','Meera','Priya','Sneha','Aditi'];
  const IN_LAST_NAMES = ['Sharma','Patel','Singh','Reddy','Gupta','Mehta','Nair','Iyer','Kumar','Das','Joshi','Kapoor','Rao','Verma'];

  const ID_LOCATIONS = [
    { city: 'Jakarta Pusat', state: 'DKI Jakarta', zips: ['10110','10220','10310','10710'] },
    { city: 'Surabaya', state: 'Jawa Timur', zips: ['60111','60241','60271','60293'] },
    { city: 'Bandung', state: 'Jawa Barat', zips: ['40111','40115','40241','40286'] },
    { city: 'Medan', state: 'Sumatera Utara', zips: ['20111','20152','20212','20231'] },
    { city: 'Denpasar', state: 'Bali', zips: ['80111','80114','80221','80234'] },
    { city: 'Yogyakarta', state: 'DI Yogyakarta', zips: ['55111','55122','55224','55281'] },
    { city: 'Makassar', state: 'Sulawesi Selatan', zips: ['90111','90125','90231','90245'] }
  ];
  const ID_STREETS = ['Jl. Jenderal Sudirman','Jl. M.H. Thamrin','Jl. Diponegoro','Jl. Ahmad Yani','Jl. Gatot Subroto','Jl. Merdeka','Jl. Pemuda','Jl. Melati','Jl. Kenanga','Jl. Asia Afrika'];
  const ID_FIRST_NAMES = ['Adi','Agus','Andi','Bima','Dimas','Fajar','Rizky','Sari','Ayu','Dewi','Indah','Maya','Putri','Rina'];
  const ID_LAST_NAMES = ['Santoso','Wijaya','Setiawan','Hidayat','Pratama','Kurniawan','Saputra','Lestari','Permata','Nugroho','Wibowo','Ramadhan','Siregar','Utami'];

  const TH_LOCATIONS = [
    { city: 'Bangkok', state: 'Bangkok', zips: ['10110','10240','10330','10400'] },
    { city: 'Chiang Mai', state: 'Chiang Mai', zips: ['50000','50100','50200','50300'] },
    { city: 'Phuket', state: 'Phuket', zips: ['83000','83100','83110','83120'] },
    { city: 'Khon Kaen', state: 'Khon Kaen', zips: ['40000','40100','40260','40320'] },
    { city: 'Pattaya', state: 'Chonburi', zips: ['20150','20160','20230','20260'] },
    { city: 'Hat Yai', state: 'Songkhla', zips: ['90110','90112','90130','90250'] },
    { city: 'Nakhon Ratchasima', state: 'Nakhon Ratchasima', zips: ['30000','30130','30210','30310'] }
  ];
  const TH_STREETS = ['Sukhumvit Road','Silom Road','Rama IX Road','Phetchaburi Road','Phahonyothin Road','Charoen Krung Road','Nimmanahaeminda Road','Huay Kaew Road','Rat-U-Thit Road','Chalermprakiat Road'];
  const TH_FIRST_NAMES = ['Anan','Chai','Kittisak','Narin','Somchai','Thanawat','Araya','Benjawan','Kanya','Mali','Nicha','Pimchanok','Suda','Wipada'];
  const TH_LAST_NAMES = ['Srisuk','Chantarangsu','Kittipong','Saelim','Wongchai','Prasert','Rattanakosin','Sukhum','Thanasiri','Boonmee','Kaewmanee','Panyasiri','Suwan','Thongchai'];

  const KR_LOCATIONS = [
    { city: 'Seoul', state: 'Seoul', zips: ['03027','04524','06035','06164'] },
    { city: 'Busan', state: 'Busan', zips: ['47291','47545','48058','48942'] },
    { city: 'Incheon', state: 'Incheon', zips: ['21354','21554','21984','22382'] },
    { city: 'Daegu', state: 'Daegu', zips: ['41911','42183','42412','42838'] },
    { city: 'Daejeon', state: 'Daejeon', zips: ['34126','34838','35229','35412'] },
    { city: 'Gwangju', state: 'Gwangju', zips: ['61186','61475','61945','62366'] },
    { city: 'Suwon', state: 'Gyeonggi-do', zips: ['16229','16491','16622','16705'] }
  ];
  const KR_STREETS = ['Teheran-ro','Sejong-daero','Gangnam-daero','Eulji-ro','Jong-ro','Haeundae-ro','Centum jungang-ro','Songdo-gukje-daero','Dunsan-ro','Paldal-ro'];
  const KR_FIRST_NAMES = ['Min-jun','Seo-jun','Ji-ho','Do-yun','Hyun-woo','Seo-yeon','Ji-woo','Ha-eun','Soo-ah','Ye-eun','Yu-na','Min-seo','Joon-ho','Hye-jin'];
  const KR_LAST_NAMES = ['Kim','Lee','Park','Choi','Jung','Kang','Cho','Yoon','Jang','Lim','Han','Shin','Song','Kwon'];

  const CH_LOCATIONS = [
    { city: 'Zürich', state: 'Zürich', zips: ['8001','8002','8004','8050'] },
    { city: 'Geneva', state: 'Genève', zips: ['1201','1204','1205','1207'] },
    { city: 'Basel', state: 'Basel-Stadt', zips: ['4001','4051','4056','4058'] },
    { city: 'Bern', state: 'Bern', zips: ['3001','3011','3012','3014'] },
    { city: 'Lausanne', state: 'Vaud', zips: ['1003','1004','1006','1010'] },
    { city: 'Lucerne', state: 'Luzern', zips: ['6003','6004','6005','6014'] },
    { city: 'Lugano', state: 'Ticino', zips: ['6900','6902','6904','6962'] }
  ];
  const CH_STREETS = ['Bahnhofstrasse','Seefeldstrasse','Rue du Rhône','Avenue de la Gare','Marktgasse','Spitalgasse','Freie Strasse','Via Nassa','Pilatusstrasse','Bundesgasse'];
  const CH_FIRST_NAMES = ['Noah','Liam','Luca','Leon','Matteo','Julian','Emma','Mia','Sofia','Lena','Laura','Lea','Nina','Elena'];
  const CH_LAST_NAMES = ['Müller','Meier','Schmid','Keller','Weber','Frei','Huber','Rossi','Bernasconi','Favre','Dubois','Morel','Steiner','Brunner'];

  const SG_LOCATIONS = [
    { city: 'Marina Bay', state: 'Central Region', zips: ['018956','018989','019396','039797'] },
    { city: 'Orchard', state: 'Central Region', zips: ['228208','238839','238877','239693'] },
    { city: 'Tampines', state: 'East Region', zips: ['529510','529536','529684','529757'] },
    { city: 'Jurong East', state: 'West Region', zips: ['608550','609601','609731','609732'] },
    { city: 'Woodlands', state: 'North Region', zips: ['730888','738099','738343','739065'] },
    { city: 'Serangoon', state: 'North-East Region', zips: ['550201','554369','556083','556119'] },
    { city: 'Queenstown', state: 'Central Region', zips: ['140145','148951','149053','159919'] }
  ];
  const SG_STREETS = ['Orchard Road','Marina Boulevard','Tampines Central','Jurong East Street 21','Woodlands Avenue 3','Bukit Timah Road','Alexandra Road','Serangoon Road','Paya Lebar Road','Clementi Avenue 2'];
  const SG_FIRST_NAMES = ['Wei Ming','Jia Hao','Jun Jie','Kai Wen','Daniel','Arjun','Mei Ling','Jia Yi','Aisha','Nurul','Priya','Xin Yi','Hui Min','Sarah'];
  const SG_LAST_NAMES = ['Tan','Lim','Lee','Ng','Wong','Goh','Koh','Chan','Rahman','Nair','Ong','Teo','Chua','Ho'];

  const PL_LOCATIONS = [
    { city: 'Warsaw', state: 'Mazowieckie', zips: ['00-001','00-019','00-950','01-001'] },
    { city: 'Kraków', state: 'Małopolskie', zips: ['30-001','30-002','30-062','31-001'] },
    { city: 'Wrocław', state: 'Dolnośląskie', zips: ['50-001','50-010','50-101','51-001'] },
    { city: 'Gdańsk', state: 'Pomorskie', zips: ['80-001','80-009','80-803','80-831'] },
    { city: 'Poznań', state: 'Wielkopolskie', zips: ['60-001','60-101','61-001','61-738'] },
    { city: 'Łódź', state: 'Łódzkie', zips: ['90-001','90-004','91-001','93-001'] },
    { city: 'Katowice', state: 'Śląskie', zips: ['40-001','40-005','40-098','40-101'] }
  ];
  const PL_STREETS = ['Marszałkowska','Nowy Świat','Aleje Jerozolimskie','Floriańska','Długa','Piotrkowska','Świdnicka','Grunwaldzka','Półwiejska','Mariacka'];
  const PL_FIRST_NAMES = ['Jan','Piotr','Jakub','Michał','Tomasz','Krzysztof','Anna','Zofia','Julia','Aleksandra','Katarzyna','Natalia','Maja','Alicja'];
  const PL_LAST_NAMES = ['Nowak','Kowalski','Wiśniewski','Wójcik','Kowalczyk','Kamiński','Lewandowski','Zieliński','Szymański','Woźniak','Dąbrowski','Kozłowski','Jankowski','Mazur'];

  const MY_LOCATIONS = [
    { city: 'Kuala Lumpur', state: 'Kuala Lumpur', zips: ['50000','50100','50200','50450'] },
    { city: 'George Town', state: 'Penang', zips: ['10000','10200','10350','10450'] },
    { city: 'Johor Bahru', state: 'Johor', zips: ['80000','80200','80300','81200'] },
    { city: 'Shah Alam', state: 'Selangor', zips: ['40000','40100','40150','40300'] },
    { city: 'Ipoh', state: 'Perak', zips: ['30000','30200','30300','31400'] },
    { city: 'Kota Kinabalu', state: 'Sabah', zips: ['88000','88100','88300','88400'] },
    { city: 'Kuching', state: 'Sarawak', zips: ['93000','93100','93200','93350'] }
  ];
  const MY_STREETS = ['Jalan Ampang','Jalan Bukit Bintang','Jalan Sultan Ismail','Jalan Tun Razak','Jalan Macalister','Jalan Wong Ah Fook','Persiaran Kayangan','Jalan Gopeng','Jalan Lintas','Jalan Padungan'];
  const MY_FIRST_NAMES = ['Amir','Hakim','Daniel','Wei Jian','Jia Hao','Arjun','Aisyah','Nurul','Siti','Mei Ling','Priya','Farah','Xin Yi','Kavitha'];
  const MY_LAST_NAMES = ['Rahman','Ismail','Abdullah','Tan','Lim','Lee','Wong','Goh','Nair','Kumar','Ong','Chan','Yap','Singh'];

  const NL_LOCATIONS = [
    { city: 'Amsterdam', state: 'Noord-Holland', zips: ['1011 AB','1012 JS','1054 EA','1071 DJ'] },
    { city: 'Rotterdam', state: 'Zuid-Holland', zips: ['3011 AA','3012 AD','3021 HC','3072 AP'] },
    { city: 'The Hague', state: 'Zuid-Holland', zips: ['2511 AA','2514 CE','2562 AW','2585 EV'] },
    { city: 'Utrecht', state: 'Utrecht', zips: ['3511 AA','3512 JC','3521 AL','3572 CE'] },
    { city: 'Eindhoven', state: 'Noord-Brabant', zips: ['5611 AA','5612 AZ','5616 CA','5621 AA'] },
    { city: 'Groningen', state: 'Groningen', zips: ['9711 AA','9712 CP','9721 AD','9741 AA'] },
    { city: 'Maastricht', state: 'Limburg', zips: ['6211 AA','6212 AR','6221 AA','6224 EA'] }
  ];
  const NL_STREETS = ['Damrak','Keizersgracht','Prinsengracht','Coolsingel','Laan van Meerdervoort','Oudegracht','Strijp-S','Grote Markt','Vrijthof','Witte de Withstraat'];
  const NL_FIRST_NAMES = ['Daan','Sem','Lucas','Finn','Lars','Bram','Emma','Sophie','Julia','Mila','Tess','Lotte','Nora','Eva'];
  const NL_LAST_NAMES = ['de Jong','Jansen','de Vries','van den Berg','van Dijk','Bakker','Visser','Smit','Meijer','Bos','Vos','Peters','Hendriks','Dekker'];

  const AE_LOCATIONS = [
    { city: 'Dubai', state: 'Dubai', zips: ['00000'] },
    { city: 'Abu Dhabi', state: 'Abu Dhabi', zips: ['00000'] },
    { city: 'Sharjah', state: 'Sharjah', zips: ['00000'] },
    { city: 'Ajman', state: 'Ajman', zips: ['00000'] },
    { city: 'Ras Al Khaimah', state: 'Ras Al Khaimah', zips: ['00000'] },
    { city: 'Fujairah', state: 'Fujairah', zips: ['00000'] },
    { city: 'Al Ain', state: 'Abu Dhabi', zips: ['00000'] }
  ];
  const AE_STREETS = ['Sheikh Zayed Road','Corniche Road','Al Wasl Road','Jumeirah Road','Al Khaleej Street','Airport Road','King Faisal Street','Al Ittihad Road','Khalifa Street','Al Maktoum Road'];
  const AE_FIRST_NAMES = ['Omar','Khalid','Ahmed','Youssef','Saeed','Hamad','Fatima','Aisha','Mariam','Noor','Layla','Hind','Sara','Amna'];
  const AE_LAST_NAMES = ['Al Mansoori','Al Nuaimi','Al Mazrouei','Al Falasi','Al Suwaidi','Al Zaabi','Hassan','Rahman','Al Marri','Al Shamsi','Al Ketbi','Al Muhairi','Al Hammadi','Al Dhaheri'];

  const AT_LOCATIONS = [
    { city: 'Vienna', state: 'Wien', zips: ['1010','1020','1030','1070'] },
    { city: 'Salzburg', state: 'Salzburg', zips: ['5020','5023','5026','5071'] },
    { city: 'Graz', state: 'Steiermark', zips: ['8010','8020','8036','8041'] },
    { city: 'Innsbruck', state: 'Tirol', zips: ['6020','6063','6080','6091'] },
    { city: 'Linz', state: 'Oberösterreich', zips: ['4020','4030','4040','4060'] },
    { city: 'Klagenfurt', state: 'Kärnten', zips: ['9020','9061','9063','9073'] },
    { city: 'Bregenz', state: 'Vorarlberg', zips: ['6900','6911','6923','6971'] }
  ];
  const AT_STREETS = ['Mariahilfer Straße','Kärntner Straße','Landstraße','Getreidegasse','Herrengasse','Museumstraße','Hauptplatz','Anichstraße','Bahnhofstraße','Seestraße'];
  const AT_FIRST_NAMES = ['Lukas','Leon','Paul','Felix','Jonas','Maximilian','Anna','Emma','Marie','Laura','Lena','Sophie','Mia','Julia'];
  const AT_LAST_NAMES = ['Gruber','Huber','Wagner','Müller','Pichler','Moser','Steiner','Bauer','Hofer','Berger','Fuchs','Mayer','Schmid','Weber'];

  const DE_LOCATIONS = [
    { city: 'Berlin', state: 'Berlin', zips: ['10115','10117','10405','10999'] },
    { city: 'Munich', state: 'Bayern', zips: ['80331','80333','80538','80802'] },
    { city: 'Hamburg', state: 'Hamburg', zips: ['20095','20144','20457','22765'] },
    { city: 'Frankfurt am Main', state: 'Hessen', zips: ['60311','60313','60322','60594'] },
    { city: 'Cologne', state: 'Nordrhein-Westfalen', zips: ['50667','50674','50823','50931'] },
    { city: 'Stuttgart', state: 'Baden-Württemberg', zips: ['70173','70176','70327','70563'] },
    { city: 'Dresden', state: 'Sachsen', zips: ['01067','01069','01127','01219'] }
  ];
  const DE_STREETS = ['Friedrichstraße','Kurfürstendamm','Leopoldstraße','Mönckebergstraße','Zeil','Schildergasse','Königstraße','Prager Straße','Hauptstraße','Bahnhofstraße'];
  const DE_FIRST_NAMES = ['Leon','Paul','Jonas','Felix','Lukas','Finn','Emma','Mia','Hannah','Emilia','Lina','Sophie','Anna','Lea'];
  const DE_LAST_NAMES = ['Müller','Schmidt','Schneider','Fischer','Weber','Meyer','Wagner','Becker','Schulz','Hoffmann','Koch','Richter','Bauer','Klein'];

  const UA_LOCATIONS = [
    { city: 'Kyiv', state: 'Kyiv City', zips: ['01001','01010','02000','03150'] },
    { city: 'Lviv', state: 'Lviv Oblast', zips: ['79000','79005','79019','79035'] },
    { city: 'Odesa', state: 'Odesa Oblast', zips: ['65000','65012','65026','65045'] },
    { city: 'Kharkiv', state: 'Kharkiv Oblast', zips: ['61000','61002','61022','61103'] },
    { city: 'Dnipro', state: 'Dnipropetrovsk Oblast', zips: ['49000','49005','49027','49101'] },
    { city: 'Vinnytsia', state: 'Vinnytsia Oblast', zips: ['21000','21007','21018','21036'] },
    { city: 'Ivano-Frankivsk', state: 'Ivano-Frankivsk Oblast', zips: ['76000','76008','76018','76026'] }
  ];
  const UA_STREETS = ['Khreshchatyk Street','Velyka Vasylkivska Street','Svobody Avenue','Deribasivska Street','Sumska Street','Dmytra Yavornytskoho Avenue','Soborna Street','Nezalezhnosti Street','Hrushevskoho Street','Shevchenka Street'];
  const UA_FIRST_NAMES = ['Oleksandr','Andrii','Dmytro','Maksym','Artem','Bohdan','Anna','Sofiia','Olena','Kateryna','Mariia','Iryna','Yuliia','Anastasiia'];
  const UA_LAST_NAMES = ['Shevchenko','Kovalenko','Bondarenko','Tkachenko','Kovalchuk','Kravchenko','Oliinyk','Melnyk','Boyko','Polishchuk','Lysenko','Savchenko','Rudenko','Petrenko'];

  const VN_LOCATIONS = [
    { city: 'Ho Chi Minh City', state: 'Ho Chi Minh City', zips: ['700000','700100','700200','700300'] },
    { city: 'Hanoi', state: 'Hanoi', zips: ['100000','100100','100200','100300'] },
    { city: 'Da Nang', state: 'Da Nang', zips: ['550000','550100','550200','550300'] },
    { city: 'Hai Phong', state: 'Hai Phong', zips: ['570000','570100','570200','570300'] },
    { city: 'Nha Trang', state: 'Khanh Hoa', zips: ['650000','650100','650200','650300'] },
    { city: 'Can Tho', state: 'Can Tho', zips: ['900000','900100','900200','900300'] },
    { city: 'Hue', state: 'Thua Thien Hue', zips: ['530000','530100','530200','530300'] }
  ];
  const VN_STREETS = ['Nguyen Hue Street','Le Loi Street','Tran Hung Dao Street','Hai Ba Trung Street','Vo Van Tan Street','Nguyen Trai Street','Pham Ngu Lao Street','Ba Trieu Street','Ly Thuong Kiet Street','Dien Bien Phu Street'];
  const VN_FIRST_NAMES = ['Minh','Anh','Huy','Nam','Tuan','Long','Linh','Trang','Mai','Thao','Lan','Phuong','Ngoc','Ha'];
  const VN_LAST_NAMES = ['Nguyen','Tran','Le','Pham','Hoang','Huynh','Phan','Vu','Vo','Dang','Bui','Do','Ho','Ngo'];

  const PH_LOCATIONS = [
    { city: 'Manila', state: 'Metro Manila', zips: ['1000','1004','1006','1012'] },
    { city: 'Quezon City', state: 'Metro Manila', zips: ['1100','1101','1103','1110'] },
    { city: 'Makati', state: 'Metro Manila', zips: ['1200','1204','1209','1227'] },
    { city: 'Pasig', state: 'Metro Manila', zips: ['1600','1603','1605','1610'] },
    { city: 'Cebu City', state: 'Cebu', zips: ['6000','6004','6006','6014'] },
    { city: 'Davao City', state: 'Davao del Sur', zips: ['8000','8002','8004','8016'] },
    { city: 'Baguio', state: 'Benguet', zips: ['2600','2601','2602','2604'] }
  ];
  const PH_STREETS = ['Ayala Avenue','Epifanio de los Santos Avenue','Roxas Boulevard','Taft Avenue','Makati Avenue','Ortigas Avenue','Commonwealth Avenue','Osmena Boulevard','Claveria Street','Session Road'];
  const PH_FIRST_NAMES = ['Juan','Jose','Miguel','Gabriel','Paolo','Carlos','Maria','Ana','Angela','Sofia','Isabella','Camille','Patricia','Bianca'];
  const PH_LAST_NAMES = ['Santos','Reyes','Cruz','Garcia','Mendoza','Bautista','Flores','Aquino','Ramos','Navarro','Castillo','Torres','Rivera','Villanueva'];

  const BA_LOCATIONS = [
    { city: 'Sarajevo', state: 'Federation of Bosnia and Herzegovina', zips: ['71000','71010','71120','71210'] },
    { city: 'Banja Luka', state: 'Republika Srpska', zips: ['78000','78010','78101','78250'] },
    { city: 'Mostar', state: 'Federation of Bosnia and Herzegovina', zips: ['88000','88101','88201','88240'] },
    { city: 'Tuzla', state: 'Federation of Bosnia and Herzegovina', zips: ['75000','75010','75201','75270'] },
    { city: 'Zenica', state: 'Federation of Bosnia and Herzegovina', zips: ['72000','72010','72220','72240'] },
    { city: 'Bijeljina', state: 'Republika Srpska', zips: ['76300','76310','76320','76330'] },
    { city: 'Brcko', state: 'Brcko District', zips: ['76100','76101','76200','76230'] }
  ];
  const BA_STREETS = ['Marsala Tita','Ferhadija','Zmaja od Bosne','Kralja Petra I','Alekse Santica','Bulevar Mira','Mehmeda Spahe','Obala Kulina bana','Mese Selimovica','Branilaca Sarajeva'];
  const BA_FIRST_NAMES = ['Amar','Emir','Haris','Adnan','Tarik','Mirza','Amina','Lejla','Sara','Emina','Nina','Ajla','Merima','Lamija'];
  const BA_LAST_NAMES = ['Hodzic','Kovacevic','Markovic','Petrovic','Basic','Hadžic','Dedic','Ilic','Jovanovic','Nikolic','Begic','Halilovic','Softic','Memic'];

  const BH_LOCATIONS = [
    { city: 'Manama', state: 'Capital Governorate', zips: ['317','318','321','338'] },
    { city: 'Muharraq', state: 'Muharraq Governorate', zips: ['202','203','207','224'] },
    { city: 'Riffa', state: 'Southern Governorate', zips: ['901','903','905','909'] },
    { city: 'Isa Town', state: 'Southern Governorate', zips: ['801','803','806','812'] },
    { city: 'Hamad Town', state: 'Northern Governorate', zips: ['1205','1207','1210','1216'] },
    { city: 'Sitra', state: 'Capital Governorate', zips: ['601','603','606','611'] },
    { city: 'Budaiya', state: 'Northern Governorate', zips: ['552','553','555','559'] }
  ];
  const BH_STREETS = ['Government Avenue','Exhibition Avenue','Al Fateh Highway','Budaiya Highway','Shaikh Isa Avenue','King Faisal Highway','Road 2802','Road 3801','Road 1704','Road 1010'];
  const BH_FIRST_NAMES = ['Ahmed','Ali','Hassan','Mohammed','Yousef','Khalid','Fatima','Maryam','Noor','Aisha','Layla','Sara','Zainab','Hessa'];
  const BH_LAST_NAMES = ['Al Khalifa','Al Doseri','Al Zayani','Al Mannai','Al Noaimi','Al Sayed','Hassan','Abdullah','Rahman','Khan','Al Arrayed','Al Jalahma','Al Fardan','Al Kooheji'];

  function generateRandomAddress(country) {
    if (country === 'jp') {
      // 日本：汉字地址（都道府県 / 市区町村 / 町名+番地），邮编与市区对应
      const p = generateJpIdentity(_randomInt(0, 1000000000));
      return { street: p.addressLine, city: p.city, state: p.prefecture, zip: p.zip, town: p.town, country: 'jp' };
    }
    if (country === 'br') {
      // 巴西：城市/州/CEP + 街道名 + 门牌号 + 街区
      const location = _randomPick(BR_LOCATIONS);
      const zip = _randomPick(location.zips);
      const streetName = _randomPick(BR_STREET_NAMES);
      const streetNum = _randomInt(1, 9999);
      const bairro = _randomPick(BR_NEIGHBORHOODS);
      return { street: streetName, number: String(streetNum), city: location.city, state: location.state, zip, neighborhood: bairro, country: 'br' };
    }
    if (country === 'uk') {
      const location = _randomPick(UK_LOCATIONS);
      const street = `${_randomInt(1, 250)} ${_randomPick(UK_STREETS)}`;
      return { street, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'uk' };
    }
    if (country === 'in') {
      const location = _randomPick(IN_LOCATIONS);
      const street = `${_randomInt(1, 240)}, ${_randomPick(IN_STREETS)}`;
      return { street, number: street.split(',')[0], city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'in' };
    }
    if (country === 'id') {
      const location = _randomPick(ID_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${_randomPick(ID_STREETS)} No. ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'id' };
    }
    if (country === 'th') {
      const location = _randomPick(TH_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number} ${_randomPick(TH_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'th' };
    }
    if (country === 'kr') {
      const location = _randomPick(KR_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number}, ${_randomPick(KR_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'kr' };
    }
    if (country === 'ch') {
      const location = _randomPick(CH_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${_randomPick(CH_STREETS)} ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'ch' };
    }
    if (country === 'sg') {
      const location = _randomPick(SG_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number} ${_randomPick(SG_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'sg' };
    }
    if (country === 'pl') {
      const location = _randomPick(PL_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `ul. ${_randomPick(PL_STREETS)} ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'pl' };
    }
    if (country === 'my') {
      const location = _randomPick(MY_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number}, ${_randomPick(MY_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'my' };
    }
    if (country === 'nl') {
      const location = _randomPick(NL_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${_randomPick(NL_STREETS)} ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'nl' };
    }
    if (country === 'ae') {
      const location = _randomPick(AE_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number}, ${_randomPick(AE_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'ae' };
    }
    if (country === 'at') {
      const location = _randomPick(AT_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${_randomPick(AT_STREETS)} ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'at' };
    }
    if (country === 'de') {
      const location = _randomPick(DE_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${_randomPick(DE_STREETS)} ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'de' };
    }
    if (country === 'ua') {
      const location = _randomPick(UA_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number}, ${_randomPick(UA_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'ua' };
    }
    if (country === 'vn') {
      const location = _randomPick(VN_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number} ${_randomPick(VN_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'vn' };
    }
    if (country === 'ph') {
      const location = _randomPick(PH_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number} ${_randomPick(PH_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'ph' };
    }
    if (country === 'ba') {
      const location = _randomPick(BA_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${_randomPick(BA_STREETS)} ${number}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'ba' };
    }
    if (country === 'bh') {
      const location = _randomPick(BH_LOCATIONS);
      const number = String(_randomInt(1, 240));
      return { street: `${number}, ${_randomPick(BH_STREETS)}`, number, city: location.city, state: location.state, zip: _randomPick(location.zips), country: 'bh' };
    }
    const location = _randomPick(TAX_FREE_LOCATIONS);
    const zip = _randomPick(location.zips);
    const streetNum = _randomInt(100, 9999);
    const usePrefix = Math.random() < 0.3;
    const prefix = usePrefix ? _randomPick(STREET_PREFIXES) + " " : "";
    const streetName = _randomPick(STREET_NAMES);
    const street = streetNum + " " + prefix + streetName;
    return { street, city: location.city, state: location.state, zip, country: 'us' };
  }

  function generateRandomName(country) {
    if (country === 'jp') {
      // 日本：汉字本名 + 假名读音（カナ/かな），供「氏名」与「フリガナ」两组字段
      const p = generateJpIdentity(_randomInt(0, 1000000000));
      return {
        firstName: p.firstName, lastName: p.lastName, name: p.name,
        firstNameKana: p.firstNameKana, lastNameKana: p.lastNameKana, nameKana: p.nameKana,
        firstNameHira: p.firstNameHira, lastNameHira: p.lastNameHira, nameHira: p.nameHira,
        country: 'jp'
      };
    }
    if (country === 'br') {
      // 巴西：葡萄牙语姓名（姓可带双姓）
      const firstName = _randomPick(BR_FIRST_NAMES);
      const lastName = _randomPick(BR_LAST_NAMES);
      const secondLastName = Math.random() < 0.5 ? _randomPick(BR_LAST_NAMES) : '';
      const fullLastName = secondLastName ? `${lastName} ${secondLastName}` : lastName;
      return { firstName, lastName: fullLastName, name: `${firstName} ${fullLastName}`, country: 'br' };
    }
    if (country === 'uk') {
      const firstName = _randomPick(UK_FIRST_NAMES);
      const lastName = _randomPick(UK_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'uk' };
    }
    if (country === 'in') {
      const firstName = _randomPick(IN_FIRST_NAMES);
      const lastName = _randomPick(IN_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'in' };
    }
    if (country === 'id') {
      const firstName = _randomPick(ID_FIRST_NAMES);
      const lastName = _randomPick(ID_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'id' };
    }
    if (country === 'th') {
      const firstName = _randomPick(TH_FIRST_NAMES);
      const lastName = _randomPick(TH_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'th' };
    }
    if (country === 'kr') {
      const firstName = _randomPick(KR_FIRST_NAMES);
      const lastName = _randomPick(KR_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'kr' };
    }
    if (country === 'ch') {
      const firstName = _randomPick(CH_FIRST_NAMES);
      const lastName = _randomPick(CH_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'ch' };
    }
    if (country === 'sg') {
      const firstName = _randomPick(SG_FIRST_NAMES);
      const lastName = _randomPick(SG_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'sg' };
    }
    if (country === 'pl') {
      const firstName = _randomPick(PL_FIRST_NAMES);
      const lastName = _randomPick(PL_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'pl' };
    }
    if (country === 'my') {
      const firstName = _randomPick(MY_FIRST_NAMES);
      const lastName = _randomPick(MY_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'my' };
    }
    if (country === 'nl') {
      const firstName = _randomPick(NL_FIRST_NAMES);
      const lastName = _randomPick(NL_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'nl' };
    }
    if (country === 'ae') {
      const firstName = _randomPick(AE_FIRST_NAMES);
      const lastName = _randomPick(AE_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'ae' };
    }
    if (country === 'at') {
      const firstName = _randomPick(AT_FIRST_NAMES);
      const lastName = _randomPick(AT_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'at' };
    }
    if (country === 'de') {
      const firstName = _randomPick(DE_FIRST_NAMES);
      const lastName = _randomPick(DE_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'de' };
    }
    if (country === 'ua') {
      const firstName = _randomPick(UA_FIRST_NAMES);
      const lastName = _randomPick(UA_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'ua' };
    }
    if (country === 'vn') {
      const firstName = _randomPick(VN_FIRST_NAMES);
      const lastName = _randomPick(VN_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'vn' };
    }
    if (country === 'ph') {
      const firstName = _randomPick(PH_FIRST_NAMES);
      const lastName = _randomPick(PH_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'ph' };
    }
    if (country === 'ba') {
      const firstName = _randomPick(BA_FIRST_NAMES);
      const lastName = _randomPick(BA_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'ba' };
    }
    if (country === 'bh') {
      const firstName = _randomPick(BH_FIRST_NAMES);
      const lastName = _randomPick(BH_LAST_NAMES);
      return { firstName, lastName, name: `${firstName} ${lastName}`, country: 'bh' };
    }
    const firstName = _randomPick(FIRST_NAMES);
    const lastName = _randomPick(LAST_NAMES);
    return { firstName, lastName, name: firstName + " " + lastName, country: 'us' };
  }

  // 美国真实存在的发卡行 BIN（前6位）+ 卡长度
  // 来源：公开 IIN/BIN 数据库中各大美国发卡行（Chase / BofA / Wells Fargo / Citi / Capital One /
  // US Bank / PNC / Discover / AmEx / Synchrony / Navy Federal CU 等）的常用 BIN
  const US_CARD_BINS = [
    // 只用 Chase Visa 414709
    { bin: '414709', length: 16, brand: 'Visa', issuer: 'Chase' }
  ];

  // 日本主要发卡行 BIN（前6位）+ 卡长度
  // JCB / Visa（Mizuho/SMBC/MUFG/Rakuten/AEON/Epos）/ Mastercard（Orico/JACCS/AEON）/ Amex Japan
  const JP_CARD_BINS = [
    // 只用 Chase Visa 414709
    { bin: '414709', length: 16, brand: 'Visa', issuer: 'Chase' }
  ];

  // 巴西主要发卡行 BIN（前6位）+ 卡长度
  // Visa / Mastercard（Nubank / Itaú / Bradesco / Banco do Brasil / Santander Brasil / Banco Inter / C6 Bank）
  const BR_CARD_BINS = [
    { bin: '516292', length: 16, brand: 'Mastercard', issuer: 'Nubank' },
    { bin: '530301', length: 16, brand: 'Mastercard', issuer: 'Banco Inter' },
    { bin: '553636', length: 16, brand: 'Mastercard', issuer: 'C6 Bank' },
    { bin: '423456', length: 16, brand: 'Visa', issuer: 'Itaú' },
    { bin: '492405', length: 16, brand: 'Visa', issuer: 'Bradesco' },
    { bin: '498401', length: 16, brand: 'Visa', issuer: 'Banco do Brasil' },
    { bin: '485462', length: 16, brand: 'Visa', issuer: 'Santander Brasil' },
  ];

  // 日本主要城市/邮编（"###-####" 格式，对应实际行政区）
  const JP_LOCATIONS = [
    { city: 'Chiyoda-ku', prefecture: 'Tokyo', zips: ['100-0001','100-0002','100-0004','100-0005','100-0011','100-0013','101-0021','101-0032','101-0051','102-0073','102-0082','102-0093'] },
    { city: 'Minato-ku', prefecture: 'Tokyo', zips: ['105-0001','105-0011','105-0014','106-0031','106-0032','106-0041','106-0044','106-0045','106-0046','106-0047','107-0051','107-0052','107-0061','108-0014','108-0023'] },
    { city: 'Shibuya-ku', prefecture: 'Tokyo', zips: ['150-0001','150-0002','150-0011','150-0012','150-0013','150-0021','150-0031','150-0041','150-0042','150-0043','150-0044','150-0045','150-0046'] },
    { city: 'Shinjuku-ku', prefecture: 'Tokyo', zips: ['160-0001','160-0011','160-0021','160-0022','160-0023','160-0024','161-0031','162-0801','162-0814','162-0825','162-0843'] },
    { city: 'Setagaya-ku', prefecture: 'Tokyo', zips: ['154-0001','154-0011','154-0023','155-0031','155-0033','157-0061','158-0091','158-0094','156-0043','156-0044'] },
    { city: 'Meguro-ku', prefecture: 'Tokyo', zips: ['152-0001','152-0011','152-0021','152-0031','153-0041','153-0051','153-0061','153-0062','153-0063'] },
    { city: 'Toshima-ku', prefecture: 'Tokyo', zips: ['170-0001','170-0011','170-0013','170-0021','170-0031','171-0021','171-0022','171-0031','171-0041','171-0051'] },
    { city: 'Yokohama', prefecture: 'Kanagawa', zips: ['220-0001','220-0011','220-0012','221-0801','221-0802','221-0822','231-0001','231-0011','231-0023','231-0031','231-0045','232-0001'] },
    { city: 'Kawasaki', prefecture: 'Kanagawa', zips: ['210-0001','210-0006','210-0011','210-0012','212-0011','212-0013','212-0023','213-0001','213-0011','215-0004'] },
    { city: 'Saitama', prefecture: 'Saitama', zips: ['330-0001','330-0011','330-0021','330-0031','330-0041','330-0051','336-0001','336-0011','336-0021','336-0031'] },
    { city: 'Chiba', prefecture: 'Chiba', zips: ['260-0001','260-0011','260-0021','260-0031','260-0041','261-0001','261-0011','261-0021','263-0001','263-0011'] },
    { city: 'Osaka', prefecture: 'Osaka', zips: ['530-0001','530-0011','530-0021','531-0061','531-0072','541-0041','541-0051','542-0081','542-0082','550-0001','550-0011','550-0014'] },
    { city: 'Kyoto', prefecture: 'Kyoto', zips: ['600-8001','600-8011','600-8021','600-8031','604-0001','604-0011','604-0021','604-0091','604-8005','605-0001','605-0073','605-0801'] },
    { city: 'Kobe', prefecture: 'Hyogo', zips: ['650-0001','650-0011','650-0021','650-0031','650-0041','651-0001','651-0011','651-0078','651-0086','651-0094'] },
    { city: 'Nagoya', prefecture: 'Aichi', zips: ['450-0001','450-0002','450-0011','450-0021','450-0031','451-0011','451-0021','451-0031','460-0001','460-0008','460-0011'] },
    { city: 'Sapporo', prefecture: 'Hokkaido', zips: ['060-0001','060-0011','060-0021','060-0031','060-0041','060-0051','060-0061','060-0807','060-0808','064-0801'] },
    { city: 'Fukuoka', prefecture: 'Fukuoka', zips: ['810-0001','810-0011','810-0021','810-0031','810-0041','812-0011','812-0013','812-0018','812-0023','813-0001'] },
    { city: 'Sendai', prefecture: 'Miyagi', zips: ['980-0001','980-0011','980-0013','980-0021','980-0031','980-0801','980-0811','980-0821','983-0001','984-0011'] },
    { city: 'Hiroshima', prefecture: 'Hiroshima', zips: ['730-0001','730-0011','730-0013','730-0021','730-0031','730-0041','730-0051','732-0011','732-0021','732-0822'] }
  ];

  // 巴西主要城市/州/CEP（#####-### 格式，真实巴西行政分区）
  const BR_LOCATIONS = [
    { city: 'São Paulo', state: 'SP', zips: ['01001-000','01002-000','01003-000','01310-000','01311-000','01414-000','04538-132','04543-011'] },
    { city: 'Rio de Janeiro', state: 'RJ', zips: ['20040-002','20040-003','20040-004','22041-001','22410-003','22430-010','22620-001','22630-014'] },
    { city: 'Belo Horizonte', state: 'MG', zips: ['30110-001','30110-002','30120-000','30130-003','30140-070','30140-071','30190-050','30190-060'] },
    { city: 'Salvador', state: 'BA', zips: ['40010-000','40020-000','40020-060','40140-040','40220-000','40220-310','41810-010','41810-020'] },
    { city: 'Brasília', state: 'DF', zips: ['70040-010','70040-020','70070-010','70070-020','70390-010','70390-020','70710-000','70710-010'] },
    { city: 'Curitiba', state: 'PR', zips: ['80010-010','80010-020','80020-000','80020-110','80240-000','80240-021','80410-000','80420-001'] },
    { city: 'Porto Alegre', state: 'RS', zips: ['90010-010','90010-020','90020-010','90020-090','90410-000','90420-000','90450-010','90460-001'] },
    { city: 'Recife', state: 'PE', zips: ['50010-000','50010-010','50020-010','50020-020','51011-000','51011-051','51020-000','51021-010'] },
    { city: 'Fortaleza', state: 'CE', zips: ['60055-100','60110-000','60115-000','60120-000','60120-010','60125-000','60135-000','60150-010'] },
    { city: 'Manaus', state: 'AM', zips: ['69005-010','69005-020','69010-000','69010-020','69020-010','69020-120','69040-010','69040-011'] },
    { city: 'Campinas', state: 'SP', zips: ['13010-010','13010-020','13013-000','13013-020','13015-000','13015-001','13020-000','13020-030'] },
    { city: 'São Bernardo do Campo', state: 'SP', zips: ['09606-000','09606-010','09710-000','09710-020','09720-000','09720-010','09750-000','09750-020'] },
    { city: 'Niterói', state: 'RJ', zips: ['24020-005','24020-006','24020-007','24210-000','24210-010','24210-020','24220-000','24220-010'] },
    { city: 'Florianópolis', state: 'SC', zips: ['88010-000','88010-010','88015-000','88015-010','88020-000','88020-010','88030-000','88036-002'] },
    { city: 'Goiânia', state: 'GO', zips: ['74000-010','74000-020','74015-010','74015-020','74020-010','74020-020','74030-010','74030-020'] },
  ];

  // 巴西常见街道名称（葡萄牙语）
  const BR_STREET_NAMES = [
    'Avenida Paulista','Avenida Rio Branco','Avenida Atlântica','Avenida Brasil',
    'Avenida Nossa Senhora de Copacabana','Avenida Ipiranga','Avenida 9 de Julho',
    'Avenida Presidente Vargas','Avenida Getúlio Vargas','Avenida Independência',
    'Rua das Flores','Rua do Comércio','Rua da Consolação','Rua Augusta',
    'Rua Oscar Freire','Rua 25 de Março','Rua da Praia','Rua dos Andradas',
    'Rua Bela Cintra','Rua Haddock Lobo','Rua da Assembleia','Rua 7 de Setembro',
    'Rua Voluntários da Pátria','Rua Gonçalves Dias','Rua Barão do Flamengo',
    'Rua Visconde de Pirajá','Rua Domingos de Morais','Rua Padre João Manuel',
    'Rua Estados Unidos','Rua Canadá','Rua França','Rua Inglaterra',
    'Alameda Santos','Alameda Lorena','Alameda Itu','Alameda Campinas',
    'Praça da República','Praça da Sé','Praça Tiradentes','Praça XV de Novembro',
    'Estrada do Campo Limpo','Estrada da Baronesa','Rodovia dos Imigrantes',
  ];

  // 巴西常见街区名（葡萄牙语）
  const BR_NEIGHBORHOODS = [
    'Centro','Copacabana','Ipanema','Leblon','Barra da Tijuca','Botafogo',
    'Flamengo','Lapa','Santa Teresa','Tijuca','Jardins','Moema','Pinheiros',
    'Vila Madalena','Itaim Bibi','Brooklin','Morumbi','Perdizes','Higienópolis',
    'Bela Vista','Consolação','Liberdade','Vila Mariana','Santo Amaro',
    'Savassi','Funcionários','Lourdes','Boa Viagem','Ondina','Pituba',
    'Batel','Água Verde','Asa Sul','Asa Norte','Lago Sul','Sudoeste',
  ];

  // 巴西常见名（葡萄牙语，男女混合）
  const BR_FIRST_NAMES = [
    'Bruno','Gabriel','Lucas','Mateus','Pedro','Rafael','João','Miguel',
    'Arthur','Davi','Bernardo','Heitor','Enzo','Lorenzo','Théo','Vicente',
    'Felipe','Gustavo','Henrique','Eduardo','Marcos','André','Carlos','Daniel',
    'Leonardo','Victor','Matheus','Samuel','Lucca','Nicolas','Guilherme','Caio',
    'Paulo','Francisco','Ricardo','Fernando','Antônio','José','Fábio','Diego',
    'Rodrigo','Alexandre','Roberto','Renato','Sérgio','Jorge','Otávio','Raul',
    'Ana','Beatriz','Camila','Daniela','Fernanda','Gabriela','Isabela','Juliana',
    'Larissa','Mariana','Natália','Patrícia','Rafaela','Sabrina','Tatiana','Vitória',
    'Adriana','Bianca','Carolina','Débora','Elaine','Flávia','Helena','Ingrid',
    'Jéssica','Luciana','Manuela','Nicole','Priscila','Renata','Simone','Tânia',
    'Laura','Sofia','Isabella','Manuela','Júlia','Heloísa','Luiza','Lorena',
    'Alice','Valentina','Clara','Cecília','Maitê','Maria Eduarda','Mirella','Elisa',
  ];

  // 巴西常见姓（葡萄牙语）
  const BR_LAST_NAMES = [
    'Silva','Santos','Oliveira','Souza','Pereira','Lima','Costa','Ferreira',
    'Rodrigues','Almeida','Nascimento','Araújo','Ribeiro','Carvalho','Cardoso',
    'Barros','Machado','Cavalcanti','Barbosa','Castro','Dias','Gomes','Marques',
    'Teixeira','Coelho','Freitas','Batista','Ramos','Vieira','Andrade','Mendes',
    'Pinto','Correia','Monteiro','Melo','Nunes','Lopes','Duarte','Moreira',
    'Fernandes','Campos','Leite','Cunha','Neves','Sales','Pacheco','Tavares',
    'Martins','Morais','Dantas','Rezende','Guimarães','Moura','Farias','Borges',
    'Soares','Rocha','Viana','Medeiros','Peixoto','Xavier','Santana','Macedo',
    'Siqueira','Pimentel','Magalhães','Bittencourt','Albuquerque','Montenegro',
    'Caldeira','Figueiredo','Gonçalves','Bueno','Amaral','Miranda','Azevedo','Branco',
  ];

  // 日本常见町/丁目地名（已知罗马字写法），与城市搭配生成地址
  const JP_TOWN_NAMES = [
    'Marunouchi','Otemachi','Yurakucho','Ginza','Roppongi','Akasaka','Aoyama','Omotesando',
    'Harajuku','Ebisu','Daikanyama','Nakameguro','Jiyugaoka','Shimokitazawa',
    'Yotsuya','Ichigaya','Iidabashi','Kagurazaka','Ikebukuro','Mejiro',
    'Nakano','Koenji','Asagaya','Ogikubo','Kichijoji','Mitaka',
    'Sangenjaya','Gotanda','Osaki','Tamachi','Hamamatsucho',
    'Toranomon','Kasumigaseki','Nagatacho','Kojimachi','Hirakawacho',
    'Honmachi','Umeda','Namba','Tennoji','Shinsaibashi','Kitashinchi',
    'Sannomiya','Motomachi','Kitano','Karasuma','Kawaramachi','Kiyamachi',
    'Sakae','Fushimi','Osu','Kanayama','Hakata','Tenjin','Daimyo','Yakuin',
    'Susukino','Odori','Kotodai','Aoba','Naka-ku','Higashi-ku'
  ];

  // 日本常见姓名（罗马字）
  const JP_LAST_NAMES = [
    'Sato','Suzuki','Takahashi','Tanaka','Watanabe','Ito','Yamamoto','Nakamura','Kobayashi','Kato',
    'Yoshida','Yamada','Sasaki','Yamaguchi','Matsumoto','Inoue','Kimura','Hayashi','Shimizu',
    'Yamazaki','Mori','Abe','Ikeda','Hashimoto','Yamashita','Ishikawa','Nakajima','Maeda','Fujita',
    'Ogawa','Goto','Okada','Hasegawa','Murakami','Kondo','Ishii','Sakamoto','Endo','Aoki',
    'Fujii','Nishimura','Fukuda','Ota','Miura','Fujiwara','Okamoto','Matsuda','Nakagawa','Nakano'
  ];

  // 片假名版,日本表单常要求(PayPal 等)
  const JP_LAST_NAMES_KANA = [
    'サトウ','スズキ','タカハシ','タナカ','ワタナベ','イトウ','ヤマモト','ナカムラ','コバヤシ','カトウ',
    'ヨシダ','ヤマダ','ササキ','ヤマグチ','マツモト','イノウエ','キムラ','ハヤシ','シミズ',
    'ヤマザキ','モリ','アベ','イケダ','ハシモト','ヤマシタ','イシカワ','ナカジマ','マエダ','フジタ',
    'オガワ','ゴトウ','オカダ','ハセガワ','ムラカミ','コンドウ','イシイ','サカモト','エンドウ','アオキ',
    'フジイ','ニシムラ','フクダ','オオタ','ミウラ','フジワラ','オカモト','マツダ','ナカガワ','ナカノ'
  ];

  const JP_FIRST_NAMES = [
    'Hiroshi','Takashi','Akira','Kenji','Daiki','Yuki','Sho','Ryo','Kenta','Naoki',
    'Tatsuya','Shota','Takeshi','Haruto','Sora','Hayato','Kaito','Yuto','Riku','Itsuki',
    'Ren','Tsubasa','Daisuke','Junichi','Masaki','Kohei','Ryota','Takuya','Yusuke','Takahiro',
    'Yui','Hina','Mei','Ai','Yuna','Sakura','Hana','Aoi','Kana','Mio',
    'Akiko','Yumi','Naomi','Mariko','Keiko','Ayaka','Misaki','Saki','Rina','Yuka',
    'Haruka','Nanami','Riko','Kanako','Asuka','Mayu','Honoka','Yuki','Megumi','Erika'
  ];

  // 片假名版
  const JP_FIRST_NAMES_KANA = [
    'ヒロシ','タカシ','アキラ','ケンジ','ダイキ','ユウキ','ショウ','リョウ','ケンタ','ナオキ',
    'タツヤ','ショウタ','タケシ','ハルト','ソラ','ハヤト','カイト','ユウト','リク','イツキ',
    'レン','ツバサ','ダイスケ','ジュンイチ','マサキ','コウヘイ','リョウタ','タクヤ','ユウスケ','タカヒロ',
    'ユイ','ヒナ','メイ','アイ','ユナ','サクラ','ハナ','アオイ','カナ','ミオ',
    'アキコ','ユミ','ナオミ','マリコ','ケイコ','アヤカ','ミサキ','サキ','リナ','ユカ',
    'ハルカ','ナナミ','リコ','カナコ','アスカ','マユ','ホノカ','ユキ','メグミ','エリカ'
  ];

  function _luhnCheckDigit(numWithoutCheck) {
    let sum = 0;
    let alt = true;
    for (let i = numWithoutCheck.length - 1; i >= 0; i--) {
      let n = parseInt(numWithoutCheck[i], 10);
      if (alt) {
        n *= 2;
        if (n > 9) n -= 9;
      }
      sum += n;
      alt = !alt;
    }
    const mod = sum % 10;
    return mod === 0 ? 0 : 10 - mod;
  }

  function generateRandomCardNumber(country, counter, salt) {
    // A:按国家从真实 BIN 表选卡，brand/issuer 跟随 BIN；B:salt 为每客户端持久随机盐，跨客户端解耦
    // 只出统一 16 位卡号 / 3 位 CVV 且广泛受理的卡组织(避开 Amex 15 位+4 位 CVV、Discover 受理面窄)，最大化表单兼容；要放开把品牌加回下面这行即可
    const ALLOW_BRANDS = country === 'jp' ? ['JCB', 'Visa', 'Mastercard'] : ['Visa', 'Mastercard'];
    const bins = (country === 'jp' ? JP_CARD_BINS : country === 'br' ? BR_CARD_BINS : US_CARD_BINS).filter(b => ALLOW_BRANDS.includes(b.brand));
    const saltN = (typeof salt === 'number' && isFinite(salt)) ? (salt | 0) : 0;
    let chosen, middle;
    if (typeof counter === 'number' && counter >= 0) {
      // counter 拆成「选哪个 BIN(idx) + 该 BIN 内的序号(inner)」：单客户端按号不重复，空间 ≈ BIN数 × 单BIN容量
      const c = ((Math.floor(counter) % 1000000000) + 1000000000) % 1000000000;
      const idx = c % bins.length;
      const inner = Math.floor(c / bins.length);
      chosen = bins[idx];
      const varLen = chosen.length - 7;                 // 16 位卡→9 位可变，15 位(Amex)→8 位
      const space = Math.pow(10, varLen);
      middle = String(_feistelPermute(inner % space, space, saltN)).padStart(varLen, '0');
    } else {
      chosen = _randomPick(bins);
      const varLen = chosen.length - 7;
      let s = '';
      for (let i = 0; i < varLen; i++) s += Math.floor(Math.random() * 10).toString();
      middle = s;
    }
    const partial = chosen.bin + middle;
    const check = _luhnCheckDigit(partial);
    return {
      number: partial + String(check),
      brand: chosen.brand,
      issuer: chosen.issuer,
      bin: chosen.bin,
      country: country === 'jp' ? 'jp' : country === 'br' ? 'br' : country === 'uk' ? 'uk' : 'us'
    };
  }

  // ============================================================
  //  日本身份「发号器」：汉字姓名 + 假名读音(カナ/かな) + 汉字地址
  //  目标：海量批量注册时，每个号对应唯一一套资料，按号递增不重复。
  //  关键字段(姓名+生年月日)由持久计数器经 Feistel 双射置换决定，
  //  在组合空间内「按号遍历」必不重复；地址/电话/密码再叠加随机。
  // ============================================================

  // 日本常见姓：汉字 / カタカナ / ひらがな
  const JP_SURNAMES = [
    {k:'佐藤',kana:'サトウ',hira:'さとう'},{k:'鈴木',kana:'スズキ',hira:'すずき'},{k:'高橋',kana:'タカハシ',hira:'たかはし'},
    {k:'田中',kana:'タナカ',hira:'たなか'},{k:'伊藤',kana:'イトウ',hira:'いとう'},{k:'渡辺',kana:'ワタナベ',hira:'わたなべ'},
    {k:'山本',kana:'ヤマモト',hira:'やまもと'},{k:'中村',kana:'ナカムラ',hira:'なかむら'},{k:'小林',kana:'コバヤシ',hira:'こばやし'},
    {k:'加藤',kana:'カトウ',hira:'かとう'},{k:'吉田',kana:'ヨシダ',hira:'よしだ'},{k:'山田',kana:'ヤマダ',hira:'やまだ'},
    {k:'佐々木',kana:'ササキ',hira:'ささき'},{k:'山口',kana:'ヤマグチ',hira:'やまぐち'},{k:'松本',kana:'マツモト',hira:'まつもと'},
    {k:'井上',kana:'イノウエ',hira:'いのうえ'},{k:'木村',kana:'キムラ',hira:'きむら'},{k:'林',kana:'ハヤシ',hira:'はやし'},
    {k:'斎藤',kana:'サイトウ',hira:'さいとう'},{k:'清水',kana:'シミズ',hira:'しみず'},{k:'山崎',kana:'ヤマザキ',hira:'やまざき'},
    {k:'森',kana:'モリ',hira:'もり'},{k:'阿部',kana:'アベ',hira:'あべ'},{k:'池田',kana:'イケダ',hira:'いけだ'},
    {k:'橋本',kana:'ハシモト',hira:'はしもと'},{k:'山下',kana:'ヤマシタ',hira:'やました'},{k:'石川',kana:'イシカワ',hira:'いしかわ'},
    {k:'中島',kana:'ナカジマ',hira:'なかじま'},{k:'前田',kana:'マエダ',hira:'まえだ'},{k:'藤田',kana:'フジタ',hira:'ふじた'},
    {k:'後藤',kana:'ゴトウ',hira:'ごとう'},{k:'小川',kana:'オガワ',hira:'おがわ'},{k:'岡田',kana:'オカダ',hira:'おかだ'},
    {k:'村上',kana:'ムラカミ',hira:'むらかみ'},{k:'長谷川',kana:'ハセガワ',hira:'はせがわ'},{k:'近藤',kana:'コンドウ',hira:'こんどう'},
    {k:'石井',kana:'イシイ',hira:'いしい'},{k:'坂本',kana:'サカモト',hira:'さかもと'},{k:'遠藤',kana:'エンドウ',hira:'えんどう'},
    {k:'青木',kana:'アオキ',hira:'あおき'},{k:'藤井',kana:'フジイ',hira:'ふじい'},{k:'西村',kana:'ニシムラ',hira:'にしむら'},
    {k:'福田',kana:'フクダ',hira:'ふくだ'},{k:'太田',kana:'オオタ',hira:'おおた'},{k:'三浦',kana:'ミウラ',hira:'みうら'},
    {k:'藤原',kana:'フジワラ',hira:'ふじわら'},{k:'岡本',kana:'オカモト',hira:'おかもと'},{k:'松田',kana:'マツダ',hira:'まつだ'},
    {k:'中川',kana:'ナカガワ',hira:'なかがわ'},{k:'中野',kana:'ナカノ',hira:'なかの'},{k:'原田',kana:'ハラダ',hira:'はらだ'},
    {k:'小野',kana:'オノ',hira:'おの'},{k:'田村',kana:'タムラ',hira:'たむら'},{k:'竹内',kana:'タケウチ',hira:'たけうち'},
    {k:'金子',kana:'カネコ',hira:'かねこ'},{k:'和田',kana:'ワダ',hira:'わだ'},{k:'中山',kana:'ナカヤマ',hira:'なかやま'},
    {k:'石田',kana:'イシダ',hira:'いしだ'},{k:'上田',kana:'ウエダ',hira:'うえだ'},{k:'森田',kana:'モリタ',hira:'もりた'},
    {k:'原',kana:'ハラ',hira:'はら'},{k:'柴田',kana:'シバタ',hira:'しばた'},{k:'酒井',kana:'サカイ',hira:'さかい'},
    {k:'工藤',kana:'クドウ',hira:'くどう'},{k:'横山',kana:'ヨコヤマ',hira:'よこやま'},{k:'宮崎',kana:'ミヤザキ',hira:'みやざき'},
    {k:'宮本',kana:'ミヤモト',hira:'みやもと'},{k:'内田',kana:'ウチダ',hira:'うちだ'},{k:'高木',kana:'タカギ',hira:'たかぎ'},
    {k:'安藤',kana:'アンドウ',hira:'あんどう'},{k:'島田',kana:'シマダ',hira:'しまだ'},{k:'谷口',kana:'タニグチ',hira:'たにぐち'},
    {k:'大野',kana:'オオノ',hira:'おおの'},{k:'高田',kana:'タカダ',hira:'たかだ'},{k:'丸山',kana:'マルヤマ',hira:'まるやま'},
    {k:'今井',kana:'イマイ',hira:'いまい'},{k:'河野',kana:'コウノ',hira:'こうの'},{k:'藤本',kana:'フジモト',hira:'ふじもと'},
    {k:'村田',kana:'ムラタ',hira:'むらた'},{k:'武田',kana:'タケダ',hira:'たけだ'},{k:'上野',kana:'ウエノ',hira:'うえの'},
    {k:'杉山',kana:'スギヤマ',hira:'すぎやま'},{k:'増田',kana:'マスダ',hira:'ますだ'},{k:'小島',kana:'コジマ',hira:'こじま'},
    {k:'平野',kana:'ヒラノ',hira:'ひらの'},{k:'大塚',kana:'オオツカ',hira:'おおつか'},{k:'千葉',kana:'チバ',hira:'ちば'},
    {k:'久保',kana:'クボ',hira:'くぼ'},{k:'松井',kana:'マツイ',hira:'まつい'},{k:'岩崎',kana:'イワサキ',hira:'いわさき'},
    {k:'木下',kana:'キノシタ',hira:'きのした'},{k:'野口',kana:'ノグチ',hira:'のぐち'},{k:'松尾',kana:'マツオ',hira:'まつお'},
    {k:'野村',kana:'ノムラ',hira:'のむら'},{k:'新井',kana:'アライ',hira:'あらい'}
  ];

  // 日本常见名:汉字 / カタカナ / ひらがな / 性别(m/f)
  const JP_GIVEN_NAMES = [
    {k:'大翔',kana:'ヒロト',hira:'ひろと',g:'m'},{k:'蓮',kana:'レン',hira:'れん',g:'m'},{k:'悠真',kana:'ユウマ',hira:'ゆうま',g:'m'},
    {k:'陽翔',kana:'ハルト',hira:'はると',g:'m'},{k:'樹',kana:'イツキ',hira:'いつき',g:'m'},{k:'悠人',kana:'ユウト',hira:'ゆうと',g:'m'},
    {k:'湊',kana:'ミナト',hira:'みなと',g:'m'},{k:'大和',kana:'ヤマト',hira:'やまと',g:'m'},{k:'颯太',kana:'ソウタ',hira:'そうた',g:'m'},
    {k:'翔太',kana:'ショウタ',hira:'しょうた',g:'m'},{k:'健太',kana:'ケンタ',hira:'けんた',g:'m'},{k:'太郎',kana:'タロウ',hira:'たろう',g:'m'},
    {k:'駿',kana:'シュン',hira:'しゅん',g:'m'},{k:'隼人',kana:'ハヤト',hira:'はやと',g:'m'},{k:'拓海',kana:'タクミ',hira:'たくみ',g:'m'},
    {k:'海斗',kana:'カイト',hira:'かいと',g:'m'},{k:'亮',kana:'リョウ',hira:'りょう',g:'m'},{k:'誠',kana:'マコト',hira:'まこと',g:'m'},
    {k:'大輔',kana:'ダイスケ',hira:'だいすけ',g:'m'},{k:'智也',kana:'トモヤ',hira:'ともや',g:'m'},{k:'直樹',kana:'ナオキ',hira:'なおき',g:'m'},
    {k:'翔平',kana:'ショウヘイ',hira:'しょうへい',g:'m'},{k:'雄太',kana:'ユウタ',hira:'ゆうた',g:'m'},{k:'達也',kana:'タツヤ',hira:'たつや',g:'m'},
    {k:'和也',kana:'カズヤ',hira:'かずや',g:'m'},{k:'浩',kana:'ヒロシ',hira:'ひろし',g:'m'},{k:'聡',kana:'サトシ',hira:'さとし',g:'m'},
    {k:'慎太郎',kana:'シンタロウ',hira:'しんたろう',g:'m'},{k:'圭吾',kana:'ケイゴ',hira:'けいご',g:'m'},{k:'雄大',kana:'ユウダイ',hira:'ゆうだい',g:'m'},
    {k:'勇気',kana:'ユウキ',hira:'ゆうき',g:'m'},{k:'大樹',kana:'ダイキ',hira:'だいき',g:'m'},{k:'拓也',kana:'タクヤ',hira:'たくや',g:'m'},
    {k:'純一',kana:'ジュンイチ',hira:'じゅんいち',g:'m'},{k:'博之',kana:'ヒロユキ',hira:'ひろゆき',g:'m'},{k:'剛',kana:'ツヨシ',hira:'つよし',g:'m'},
    {k:'明',kana:'アキラ',hira:'あきら',g:'m'},{k:'徹',kana:'トオル',hira:'とおる',g:'m'},{k:'隆',kana:'タカシ',hira:'たかし',g:'m'},
    {k:'正樹',kana:'マサキ',hira:'まさき',g:'m'},{k:'英樹',kana:'ヒデキ',hira:'ひでき',g:'m'},{k:'康弘',kana:'ヤスヒロ',hira:'やすひろ',g:'m'},
    {k:'陽菜',kana:'ヒナ',hira:'ひな',g:'f'},{k:'結衣',kana:'ユイ',hira:'ゆい',g:'f'},{k:'葵',kana:'アオイ',hira:'あおい',g:'f'},
    {k:'凛',kana:'リン',hira:'りん',g:'f'},{k:'美咲',kana:'ミサキ',hira:'みさき',g:'f'},{k:'愛',kana:'アイ',hira:'あい',g:'f'},
    {k:'莉子',kana:'リコ',hira:'りこ',g:'f'},{k:'美羽',kana:'ミウ',hira:'みう',g:'f'},{k:'芽依',kana:'メイ',hira:'めい',g:'f'},
    {k:'心春',kana:'コハル',hira:'こはる',g:'f'},{k:'楓',kana:'カエデ',hira:'かえで',g:'f'},{k:'杏',kana:'アン',hira:'あん',g:'f'},
    {k:'美月',kana:'ミヅキ',hira:'みづき',g:'f'},{k:'菜々子',kana:'ナナコ',hira:'ななこ',g:'f'},{k:'優奈',kana:'ユウナ',hira:'ゆうな',g:'f'},
    {k:'彩花',kana:'アヤカ',hira:'あやか',g:'f'},{k:'七海',kana:'ナナミ',hira:'ななみ',g:'f'},{k:'優花',kana:'ユウカ',hira:'ゆうか',g:'f'},
    {k:'麻衣',kana:'マイ',hira:'まい',g:'f'},{k:'由美',kana:'ユミ',hira:'ゆみ',g:'f'},{k:'恵美',kana:'エミ',hira:'えみ',g:'f'},
    {k:'香織',kana:'カオリ',hira:'かおり',g:'f'},{k:'智子',kana:'トモコ',hira:'ともこ',g:'f'},{k:'直美',kana:'ナオミ',hira:'なおみ',g:'f'},
    {k:'真由美',kana:'マユミ',hira:'まゆみ',g:'f'},{k:'恵子',kana:'ケイコ',hira:'けいこ',g:'f'},{k:'裕子',kana:'ユウコ',hira:'ゆうこ',g:'f'},
    {k:'陽子',kana:'ヨウコ',hira:'ようこ',g:'f'},{k:'明美',kana:'アケミ',hira:'あけみ',g:'f'},{k:'久美子',kana:'クミコ',hira:'くみこ',g:'f'},
    {k:'美穂',kana:'ミホ',hira:'みほ',g:'f'},{k:'舞',kana:'マイ',hira:'まい',g:'f'},{k:'絵里',kana:'エリ',hira:'えり',g:'f'},
    {k:'千夏',kana:'チナツ',hira:'ちなつ',g:'f'},{k:'桃子',kana:'モモコ',hira:'ももこ',g:'f'},{k:'瞳',kana:'ヒトミ',hira:'ひとみ',g:'f'},
    {k:'彩乃',kana:'アヤノ',hira:'あやの',g:'f'},{k:'結菜',kana:'ユイナ',hira:'ゆいな',g:'f'},{k:'莉緒',kana:'リオ',hira:'りお',g:'f'},
    {k:'美桜',kana:'ミオ',hira:'みお',g:'f'},{k:'瑞希',kana:'ミズキ',hira:'みずき',g:'f'},{k:'佳奈',kana:'カナ',hira:'かな',g:'f'},
    {k:'奈緒',kana:'ナオ',hira:'なお',g:'f'},{k:'彩香',kana:'アヤカ',hira:'あやか',g:'f'}
  ];

  // 日本汉字地址词库:都道府県 + 市区町村 + 真实邮编 + 该区真实町名
  const JP_ADDR = [
    {pref:'東京都',city:'千代田区',zips:['100-0001','100-0005','100-0011','101-0021','101-0032','101-0051','102-0073','102-0082','102-0093'],towns:['丸の内','大手町','有楽町','内幸町','霞が関','麹町','九段南','神田駿河台']},
    {pref:'東京都',city:'港区',zips:['105-0001','105-0011','106-0031','106-0032','106-0041','106-0045','107-0051','107-0061','108-0014'],towns:['六本木','赤坂','南青山','麻布十番','芝公園','白金台','虎ノ門','海岸']},
    {pref:'東京都',city:'渋谷区',zips:['150-0001','150-0002','150-0011','150-0021','150-0031','150-0041','150-0042','150-0043','150-0044'],towns:['渋谷','恵比寿','神宮前','代々木','広尾','松濤','神南','道玄坂']},
    {pref:'東京都',city:'新宿区',zips:['160-0001','160-0011','160-0021','160-0022','160-0023','161-0031','162-0801','162-0825','162-0843'],towns:['西新宿','歌舞伎町','新宿','高田馬場','神楽坂','四谷','北新宿','信濃町']},
    {pref:'東京都',city:'世田谷区',zips:['154-0001','154-0011','154-0023','155-0031','155-0033','156-0043','157-0061','158-0091','158-0094'],towns:['三軒茶屋','太子堂','成城','用賀','経堂','駒沢','桜新町','等々力']},
    {pref:'東京都',city:'目黒区',zips:['152-0001','152-0011','152-0021','152-0031','153-0041','153-0051','153-0061','153-0062','153-0063'],towns:['目黒','中目黒','自由が丘','駒場','青葉台','八雲','碑文谷','大橋']},
    {pref:'東京都',city:'豊島区',zips:['170-0001','170-0011','170-0013','170-0021','170-0031','171-0021','171-0022','171-0031','171-0041'],towns:['池袋','目白','巣鴨','大塚','駒込','南池袋','東池袋','雑司が谷']},
    {pref:'神奈川県',city:'横浜市西区',zips:['220-0001','220-0011','220-0012','221-0801','221-0802','221-0822'],towns:['みなとみらい','北幸','南幸','高島','平沼','岡野','楠町','宮崎町']},
    {pref:'神奈川県',city:'川崎市川崎区',zips:['210-0001','210-0006','210-0011','210-0012','212-0011','212-0013'],towns:['駅前本町','砂子','本町','東田町','日進町','小川町','堀之内町','旭町']},
    {pref:'埼玉県',city:'さいたま市大宮区',zips:['330-0801','330-0802','330-0803','330-0834','330-0843','330-0844','330-0845','330-0846'],towns:['吉敷町','桜木町','大門町','仲町','宮町','土手町','錦町','高鼻町']},
    {pref:'千葉県',city:'千葉市中央区',zips:['260-0001','260-0011','260-0013','260-0021','260-0026','260-0031','260-0044','260-0045'],towns:['中央','富士見','新町','栄町','本千葉町','長洲','祐光','問屋町']},
    {pref:'大阪府',city:'大阪市北区',zips:['530-0001','530-0011','530-0012','530-0013','530-0017','531-0061','531-0072'],towns:['梅田','中之島','堂島','曽根崎','天神橋','中崎西','大深町','角田町']},
    {pref:'京都府',city:'京都市中京区',zips:['604-0000','604-0801','604-0835','604-0844','604-0857','604-0862','604-0905','604-0924'],towns:['烏丸','三条','寺町','室町','両替町','御池','先斗町','河原町']},
    {pref:'兵庫県',city:'神戸市中央区',zips:['650-0001','650-0011','650-0021','650-0031','650-0034','651-0086','651-0094','651-0096'],towns:['三宮町','元町通','北野町','加納町','磯上通','琴ノ緒町','布引町','御幸通']},
    {pref:'愛知県',city:'名古屋市中区',zips:['460-0001','460-0002','460-0003','460-0007','460-0008','460-0011','460-0012','460-0022'],towns:['栄','錦','丸の内','大須','新栄町','正木','松原','金山']},
    {pref:'北海道',city:'札幌市中央区',zips:['060-0001','060-0002','060-0003','060-0004','060-0005','060-0006','060-0807','064-0801'],towns:['北一条西','大通西','南一条西','北二条西','大通東','宮の森','円山西町','北四条西']},
    {pref:'福岡県',city:'福岡市博多区',zips:['812-0011','812-0013','812-0018','812-0023','812-0026','812-0029','812-0038','812-0039'],towns:['博多駅前','博多駅東','店屋町','綱場町','下川端町','中洲','祇園町','千代']},
    {pref:'宮城県',city:'仙台市青葉区',zips:['980-0001','980-0011','980-0013','980-0014','980-0021','980-0801','980-0811','980-0821'],towns:['一番町','国分町','中央','本町','大町','春日町','上杉','二日町']},
    {pref:'広島県',city:'広島市中区',zips:['730-0011','730-0013','730-0021','730-0031','730-0032','730-0035','730-0036','730-0051'],towns:['大手町','紙屋町','八丁堀','基町','中町','袋町','幟町','鉄砲町']}
  ];

  // 通用 Feistel 双射置换：把 [0,total) 内的序号一一映射到 [0,total)，
  // 既「看起来随机」又保证「不同序号 → 不同结果」(发号不撞)。
  function _feistelPermute(index, total, salt) {
    if (total <= 1) return 0;
    let bits = Math.ceil(Math.log2(total));
    if (bits < 2) bits = 2;
    const half = Math.ceil(bits / 2);
    const mask = (1 << half) - 1;
    const domain = Math.pow(2, half * 2); // ≥ total
    const KEYS = [0x9E37, 0x7B15, 0xC2B2, 0x4F1B];
    // 每客户端持久随机盐:折进轮函数 → 不同客户端得到不同置换,跨客户端解耦(salt 缺省/0 时与原行为一致)
    const SALT_MIX = [0x85EBCA6B, 0xC2B2AE35, 0x27D4EB2F, 0x165667B1];
    const S = (typeof salt === 'number' && isFinite(salt)) ? (salt | 0) : 0;
    let x = ((index % total) + total) % total;
    for (let guard = 0; guard < 96; guard++) {
      let L = (x >>> half) & mask;
      let R = x & mask;
      for (let i = 0; i < 4; i++) {
        const f = (Math.imul(R, 2654435761) ^ KEYS[i] ^ Math.imul(S, SALT_MIX[i])) & mask;
        const nL = R;
        const nR = L ^ f;
        L = nL; R = nR;
      }
      x = ((L * (mask + 1)) + R) % domain;
      if (x < total) return x;
      // x ∈ [total, domain) → 继续在 domain 内游走（cycle-walking）
    }
    return ((index % total) + total) % total;
  }

  // 由序号组装一位日本居民（关键字段由序号双射决定，地址/电话叠加随机）
  function _composeJpPerson(seq) {
    const S = JP_SURNAMES.length;
    const G = JP_GIVEN_NAMES.length;
    const BD_MIN_Y = 1960, BD_MAX_Y = 2004;
    const bdStart = Date.UTC(BD_MIN_Y, 0, 1);
    const bdEnd = Date.UTC(BD_MAX_Y, 11, 31);
    const D = Math.floor((bdEnd - bdStart) / 86400000) + 1;
    const TOTAL = S * G * D;
    const safeSeq = ((Math.floor(seq) % TOTAL) + TOTAL) % TOTAL;
    let x = _feistelPermute(safeSeq, TOTAL);
    const sIdx = x % S; x = Math.floor(x / S);
    const gIdx = x % G; x = Math.floor(x / G);
    const dOff = x % D;
    const sur = JP_SURNAMES[sIdx];
    const giv = JP_GIVEN_NAMES[gIdx];
    const bdate = new Date(bdStart + dOff * 86400000);
    const by = bdate.getUTCFullYear();
    const mm = String(bdate.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(bdate.getUTCDate()).padStart(2, '0');
    const addr = _randomPick(JP_ADDR.filter(a => a.pref === '東京都'));
    const zip = _randomPick(addr.zips);
    const town = _randomPick(addr.towns);
    const banchiStr = `${_randomInt(1, 8)}-${_randomInt(1, 30)}-${_randomInt(1, 25)}`;
    const head = _randomPick(['090', '080', '070']);
    const phone = `${head}-${String(_randomInt(0, 9999)).padStart(4, '0')}-${String(_randomInt(0, 9999)).padStart(4, '0')}`;
    return {
      seq: safeSeq,
      gender: giv.g,
      lastName: sur.k, firstName: giv.k, name: `${sur.k} ${giv.k}`,
      lastNameKana: sur.kana, firstNameKana: giv.kana, nameKana: `${sur.kana} ${giv.kana}`,
      lastNameHira: sur.hira, firstNameHira: giv.hira, nameHira: `${sur.hira} ${giv.hira}`,
      prefecture: addr.pref, city: addr.city, town,
      addressLine: `${town}${banchiStr}`,
      addressBanchi: banchiStr,
      zip, phone,
      birthday: {
        slash: `${by}/${mm}/${dd}`,
        dash: `${by}-${mm}-${dd}`,
        compact: `${by}${mm}${dd}`,
        jp: `${by}年${mm}月${dd}日`,
        year: String(by), month: mm, day: dd,
      },
    };
  }

  // 对外：按计数器发号生成一位日本居民。
  // options.avoid 传入「最近指纹集合(姓名+生日)」时，命中则自动跳号，杜绝近期重复。
  function generateJpIdentity(counter, options) {
    const opts = options || {};
    const avoid = (opts.avoid && typeof opts.avoid.has === 'function') ? opts.avoid : null;
    let seq = (typeof counter === 'number' && counter >= 0) ? Math.floor(counter) : _randomInt(0, 1000000000);
    let person = _composeJpPerson(seq);
    if (avoid) {
      for (let tries = 0; tries < 64 && avoid.has(person.lastName + person.firstName + '|' + person.birthday.compact); tries++) {
        seq += 1;
        person = _composeJpPerson(seq);
      }
    }
    person.fingerprint = person.lastName + person.firstName + '|' + person.birthday.compact;
    person.nextSeq = seq + 1;
    return person;
  }

  function generateRandomPrefix(length) {
    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    const ts = Date.now().toString(36);
    const randomPart = Math.random().toString(36).substring(2);
    const combined = ts + randomPart;
    let prefix = chars.charAt(Math.floor(Math.random() * 26));
    for (let i = 1; i < Math.max(length, 14); i++) {
      if (i - 1 < combined.length) prefix += combined[i - 1];
      else prefix += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return prefix;
  }

  function generateRandomQQEmail(preferredLen) {
    // QQ号：10或11位数字，首位1-9
    // preferredLen: 10=仅10位, 11=仅11位, 其他=随机10或11
    const length = (preferredLen === 10 || preferredLen === 11) ? preferredLen : (Math.random() < 0.5 ? 10 : 11);
    let qq = String(Math.floor(Math.random() * 9) + 1); // 首位1-9
    for (let i = 1; i < length; i++) {
      qq += Math.floor(Math.random() * 10);
    }
    return qq + '@qq.com';
  }

  // ============================================================
  //  巴西 CPF（个人税号）生成器
  //  格式：###.###.###-##，11 位数字含校验位
  // ============================================================
  function _generateCPF() {
    // 生成前9位随机数字（首位不能全0）
    const digits = [];
    for (let i = 0; i < 9; i++) {
      digits.push(i === 0 ? _randomInt(1, 9) : _randomInt(0, 9));
    }
    // 计算第一位校验位
    let sum = 0;
    for (let i = 0; i < 9; i++) sum += digits[i] * (10 - i);
    let d1 = 11 - (sum % 11);
    if (d1 >= 10) d1 = 0;
    digits.push(d1);
    // 计算第二位校验位
    sum = 0;
    for (let i = 0; i < 10; i++) sum += digits[i] * (11 - i);
    let d2 = 11 - (sum % 11);
    if (d2 >= 10) d2 = 0;
    digits.push(d2);
    // 格式化为 ###.###.###-##
    const s = digits.join('');
    return `${s.substring(0,3)}.${s.substring(3,6)}.${s.substring(6,9)}-${s.substring(9,11)}`;
  }

  // 巴西身份生成器：姓名 + CPF + 生日（巴西格式 dd/mm/yyyy）
  function generateBrIdentity(counter) {
    const seq = (typeof counter === 'number' && counter >= 0) ? Math.floor(counter) : _randomInt(0, 1000000000);
    // 用 seq 的 hash 来选名字，保证同一 seq 出同一套
    const _seededPick = (arr, offset) => arr[Math.abs((seq + offset * 7919) % arr.length)];
    const firstName = _seededPick(BR_FIRST_NAMES, 0);
    const lastName1 = _seededPick(BR_LAST_NAMES, 1);
    const lastName2 = (seq % 3 === 0) ? _seededPick(BR_LAST_NAMES, 2) : '';
    const fullLastName = lastName2 ? `${lastName1} ${lastName2}` : lastName1;
    // 生日：巴西格式 dd/mm/yyyy
    const minYear = 1975, maxYear = 2004;
    const yearRange = maxYear - minYear + 1;
    const offYear = Math.abs((seq * 2654435761) % yearRange);
    const year = minYear + offYear;
    const offMonth = Math.abs((seq * 12820163) % 12);
    const month = offMonth + 1;
    const daysInMonth = new Date(year, month, 0).getDate();
    const offDay = Math.abs((seq * 314159) % daysInMonth);
    const day = offDay + 1;
    const dd = String(day).padStart(2, '0');
    const mm = String(month).padStart(2, '0');
    const birthday = {
      br: `${dd}/${mm}/${year}`,
      slash: `${year}/${mm}/${dd}`,
      dash: `${year}-${mm}-${dd}`,
      compact: `${year}${mm}${dd}`,
      year: String(year),
      month: mm,
      day: dd,
    };
    const cpf = _generateCPF();
    return {
      firstName, lastName: fullLastName, name: `${firstName} ${fullLastName}`,
      cpf, birthday, country: 'br'
    };
  }

  function generateRandomAge(min = 21, max = 45) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  function generateRandomBirthday(minYear = 1965, maxYear = 1995) {
    const year = Math.floor(Math.random() * (maxYear - minYear + 1)) + minYear;
    const month = Math.floor(Math.random() * 12) + 1;
    const daysInMonth = new Date(year, month, 0).getDate();
    const day = Math.floor(Math.random() * daysInMonth) + 1;
    const mm = String(month).padStart(2, '0');
    const dd = String(day).padStart(2, '0');
    return {
      slash: `${year}/${mm}/${dd}`,
      dash: `${year}-${mm}-${dd}`,
      compact: `${year}${mm}${dd}`,
      uk: `${dd}/${mm}/${year}`,
      year: String(year),
      month: mm,
      day: dd,
    };
  }

  function normalizeExpDate(value) {
    const raw = (value || '').trim();
    const match = raw.match(/^(\d{1,4})\/(\d{1,4})$/);
    if (!match) return raw;
    const left = match[1];
    const right = match[2];
    const leftNum = parseInt(left, 10);
    const rightNum = parseInt(right, 10);

    // Support inputs like 2030/2 or 2030/02 where full year comes first.
    if (left.length === 4 && rightNum >= 1 && rightNum <= 12) {
      return `${right.padStart(2, '0')}/${left.slice(-2)}`;
    }

    // Support inputs like 02/2030 where month comes first with a full year.
    if (leftNum >= 1 && leftNum <= 12 && right.length === 4) {
      return `${left.padStart(2, '0')}/${right.slice(-2)}`;
    }

    // Support raw input like 30/02 where year comes first.
    if (leftNum > 12 && rightNum >= 1 && rightNum <= 12) {
      return `${right.padStart(2, '0')}/${left.padStart(2, '0')}`;
    }

    return `${left.padStart(2, '0')}/${right}`;
  }

  // ---------------- 验证码提取（多格式 API 支持） ----------------
  // 归一化字段名：去掉所有非字母数字字符并转小写
  // 这样 "verify_code" / "verify-code" / "verifyCode" / "VerifyCode" 都会归一化为 "verifycode"
  function _normalizeKey(key) {
    return String(key || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  // 命中即视为「明确的验证码字段」
  const _DIRECT_CODE_KEYS = new Set([
    'code', 'smscode', 'verifycode', 'verifycodes', 'verificationcode', 'vericode',
    'otp', 'otpcode', 'pin', 'pincode', 'captcha', 'captchacode', 'vcode',
    'authcode', 'authnum', 'authnumber', 'dynamiccode', 'dynamicpassword',
    'validatecode', 'validationcode', 'messagecode', 'msgcode', 'randcode',
    'securitycode', 'mfacode', '2facode', 'twofactorcode', 'tfacode',
    'tokencode', 'confirmcode', 'confirmationcode', 'regcode', 'registercode',
    'logincode', 'activationcode', 'activatecode', 'resetcode',
    'emailcode', 'mailcode', 'phonecode', 'mobilecode',
    'checkcode', 'checknum', 'checknumber', 'numcode', 'numbercode',
    'onetimecode', 'onetimepassword', 'totp', 'hotp', 'oneoff',
    'verificationnumber', 'verifynumber'
  ]);

  // 命中后会进入文本扫描，从字符串里挑验证码
  // 注意：故意不收 'data' / 'value'，它们可能是任意嵌套结构，遍历分支会自然递归进入
  const _TEXT_BEARING_KEYS = new Set([
    'message', 'msg', 'content', 'body', 'text', 'subject', 'title', 'html',
    'desc', 'description', 'detail', 'details', 'info', 'note', 'notes', 'remark',
    'sms', 'smscontent', 'smstext', 'smsbody', 'mailbody', 'mailcontent', 'mailtext',
    'emailbody', 'emailcontent', 'emailtext', 'fullcontent', 'rawtext', 'plaintext',
    'preview', 'snippet', 'summary'
  ]);

  // 用来判断「数组里哪条最新」
  const _TIME_KEYS = new Set([
    'time', 'timestamp', 'ts', 'createdat', 'createtime', 'createtm', 'createdtime',
    'receivedat', 'receivetime', 'recvtime', 'recvat', 'sendat', 'sendtime', 'senttime',
    'updatedat', 'updatetime', 'date', 'datetime', 'addtime', 'inserttime', 'occurat'
  ]);

  function _parseTimeValue(v) {
    if (v == null) return -Infinity;
    if (typeof v === 'number') return isFinite(v) ? v : -Infinity;
    const s = String(v).trim();
    if (!s) return -Infinity;
    if (/^\d+$/.test(s)) {
      const n = Number(s);
      // 10 位是秒，13 位是毫秒，都接受
      return isFinite(n) ? n : -Infinity;
    }
    const t = Date.parse(s);
    return isNaN(t) ? -Infinity : t;
  }

  function _findFreshestArrayIndex(arr) {
    let maxTime = -Infinity;
    let maxIdx = -1;
    for (let i = 0; i < arr.length; i++) {
      const child = arr[i];
      if (!child || typeof child !== 'object' || Array.isArray(child)) continue;
      for (const [k, v] of Object.entries(child)) {
        if (_TIME_KEYS.has(_normalizeKey(k))) {
          const t = _parseTimeValue(v);
          if (t > maxTime) { maxTime = t; maxIdx = i; }
          break;
        }
      }
    }
    return maxIdx;
  }

  // 解码常见 HTML 实体并去掉标签 / style / script / 注释
  const _HTML_NAMED_ENTITIES = {
    amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
    middot: '·', hellip: '…', mdash: '—', ndash: '–'
  };
  function _decodeEntities(text) {
    return text.replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g, (full, body) => {
      if (body[0] === '#') {
        const hex = body[1] === 'x' || body[1] === 'X';
        const codePoint = parseInt(body.slice(hex ? 2 : 1), hex ? 16 : 10);
        if (isFinite(codePoint) && codePoint >= 0 && codePoint <= 0x10FFFF) {
          try { return String.fromCodePoint(codePoint); } catch (e) { return full; }
        }
        return full;
      }
      const named = _HTML_NAMED_ENTITIES[body.toLowerCase()];
      return named !== undefined ? named : full;
    });
  }
  function _stripHtml(text) {
    // 这两块里全是噪声，验证码不会在里面
    let s = text.replace(/<style\b[\s\S]*?<\/style\s*>/gi, ' ')
                .replace(/<script\b[\s\S]*?<\/script\s*>/gi, ' ')
                .replace(/<!--[\s\S]*?-->/g, ' ')
                .replace(/<[^>]+>/g, ' ');
    return _decodeEntities(s).replace(/\s+/g, ' ').trim();
  }
  function _looksLikeHtml(text) {
    return /<[a-zA-Z!\/]/.test(text) || /&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);/.test(text);
  }

  // 容器键：用来识别「外层 code 是状态码，里层才是验证码」这种 wrapper
  const _CONTAINER_KEYS = new Set([
    'data', 'result', 'results', 'payload', 'content', 'message', 'messages',
    'list', 'items', 'records', 'rows', 'entries', 'detail', 'details',
    'response', 'body', 'inbox', 'mail', 'mails', 'sms', 'smsList'
  ]);

  function _isPlainCode(v) {
    if (v == null) return false;
    if (typeof v === 'boolean') return false;
    const s = String(v).trim();
    return /^\d{4,8}$/.test(s);
  }

  function _stripJsonpWrapper(raw) {
    // 形如 callback({...}) / jsonp123({...}) / cb([...])
    const m = raw.match(/^[\s;]*[A-Za-z_$][\w$.]*\s*\(\s*([\s\S]+?)\s*\)\s*;?\s*$/);
    return m ? m[1] : raw;
  }

  // 从一段自由文本里挑最可能的验证码
  function _pickBestTextCode(text) {
    if (!text) return '';
    const allMatches = Array.from(text.matchAll(/\d{4,8}/g));
    if (!allMatches.length) return '';

    const keywordPattern = /(security\s*code|verification\s*code|verify\s*code|verification|sms\s*code|access\s*code|auth\s*code|one[-\s]*time|otp|pin|captcha|token|验证码|校验码|动态码|动态密码|确认码|短信码|登录码|登陆码|注册码|激活码|安全码|code\b)/i;
    const expiryPattern = /(到期时间|过期|有效期|expiry|expire|expired|valid\s*until|expires?|deadline)/i;
    const phoneContextPattern = /(phone|tel|mobile|cell|fax|whatsapp|手机|电话|号码|联系)/i;
    const datetimePattern = /\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?/;

    const candidates = allMatches.map(match => {
      const value = match[0];
      const index = match.index || 0;
      const before = text.slice(Math.max(0, index - 60), index).toLowerCase();
      const after = text.slice(index + value.length, Math.min(text.length, index + value.length + 60)).toLowerCase();
      const around = `${before}${value}${after}`;
      let score = 0;

      if (keywordPattern.test(before)) score += 5;
      if (keywordPattern.test(after)) score += 4;
      // 紧邻 ":" / "为" / "is" 之后通常就是验证码
      if (/(?:is|为|:|：|=)\s*$/.test(before)) score += 2;
      // 6 位是最常见的验证码长度
      if (value.length === 6) score += 2;
      else if (value.length === 4 || value.length === 8) score += 1;
      else if (value.length === 5 || value.length === 7) score += 0;
      // 年份 / 日期 / 电话号码上下文要降权
      if (/^(19|20)\d{2}$/.test(value)) score -= 2;
      if (expiryPattern.test(before) || expiryPattern.test(after)) score -= 3;
      if (datetimePattern.test(around)) score -= 4;
      if (phoneContextPattern.test(before)) score -= 3;
      // 处于一长串数字中间的片段不太像独立验证码
      const charBefore = text.charAt(index - 1);
      const charAfter = text.charAt(index + value.length);
      if (/\d/.test(charBefore) || /\d/.test(charAfter)) score -= 4;

      return { value, index, score };
    });

    candidates.sort((a, b) => b.score - a.score || a.index - b.index);
    return candidates[0].score > 0 ? candidates[0].value : '';
  }

  // 递归遍历 JSON 树收集候选验证码
  function _collectJsonCandidates(node, path, candidates, depth, freshnessHint) {
    if (depth > 16 || node == null) return;

    if (Array.isArray(node)) {
      const freshestIdx = _findFreshestArrayIndex(node);
      for (let i = 0; i < node.length; i++) {
        const childHint = freshestIdx === -1
          ? freshnessHint
          : (i === freshestIdx ? 'fresh' : 'stale');
        _collectJsonCandidates(node[i], path.concat(String(i)), candidates, depth + 1, childHint);
      }
      return;
    }

    if (typeof node !== 'object') return;

    const entries = Object.entries(node);
    const normKeys = entries.map(([k]) => _normalizeKey(k));
    const keySet = new Set(normKeys);
    // 状态码包装：{ code: 0, msg: 'ok', data: {...} } —— 此时外层 code 是状态而非验证码
    const looksLikeStatusWrapper = keySet.has('code')
      && (keySet.has('msg') || keySet.has('message') || keySet.has('status') || keySet.has('error'))
      && (keySet.has('data') || keySet.has('result') || keySet.has('payload') || keySet.has('content') || keySet.has('response'));

    const inContainer = path.some(p => _CONTAINER_KEYS.has(_normalizeKey(p)));

    for (let i = 0; i < entries.length; i++) {
      const [k, v] = entries[i];
      const normKey = normKeys[i];
      const newPath = path.concat(k);

      if (_isPlainCode(v)) {
        let score = 0;
        if (_DIRECT_CODE_KEYS.has(normKey)) {
          score = (normKey === 'code') ? (looksLikeStatusWrapper ? -3 : 6) : 12;
        } else if (/code$/.test(normKey)) {
          score = 9;
        } else if (/(otp|captcha|token|pin)/.test(normKey)) {
          score = 8;
        } else if (/code/.test(normKey)) {
          score = 6;
        }
        if (score !== 0) {
          if (inContainer) score += 2;
          if (freshnessHint === 'fresh') score += 4;
          else if (freshnessHint === 'stale') score -= 4;
          const sval = String(v).trim();
          if (sval.length === 6) score += 2;
          else if (sval.length === 4 || sval.length === 8) score += 1;
          candidates.push({ value: sval, score, path: newPath });
        }
      }

      if (typeof v === 'string' && _TEXT_BEARING_KEYS.has(normKey)) {
        const cleaned = _looksLikeHtml(v) ? _stripHtml(v) : v;
        const fromText = _pickBestTextCode(cleaned);
        if (fromText) {
          let score = 7;
          if (/(message|msg|body|content|text|sms|mail|email)/.test(normKey)) score += 2;
          if (inContainer) score += 1;
          if (freshnessHint === 'fresh') score += 4;
          else if (freshnessHint === 'stale') score -= 4;
          candidates.push({ value: fromText, score, path: newPath });
        }
      }

      if (v && typeof v === 'object') {
        _collectJsonCandidates(v, newPath, candidates, depth + 1, freshnessHint);
      }
    }
  }

  function _extractFromJsonValue(json) {
    // 顶层就是裸数字字符串：{ "code": ... } 之外，也支持 "123456"
    if (typeof json === 'string' && _isPlainCode(json)) return String(json).trim();
    if (typeof json === 'number' && _isPlainCode(json)) return String(json);

    const candidates = [];
    _collectJsonCandidates(json, [], candidates, 0, null);
    if (!candidates.length) return '';
    candidates.sort((a, b) => b.score - a.score);
    return candidates[0].score > 0 ? candidates[0].value : '';
  }

  // XML / HTML 标签解析（简化版，足够覆盖 <code>123456</code> 这种）
  function _extractFromXml(body) {
    const tagRe = /<\s*([a-zA-Z_][\w:.-]*)\s*(?:[^>]*)>([^<]*)<\/\s*\1\s*>/g;
    let m;
    let bestScore = 0;
    let best = '';
    while ((m = tagRe.exec(body)) !== null) {
      const tag = _normalizeKey(m[1]);
      const val = (m[2] || '').trim();
      if (!_isPlainCode(val)) continue;
      let score = 0;
      if (_DIRECT_CODE_KEYS.has(tag)) score = 10;
      else if (/code$/.test(tag)) score = 8;
      else if (/code/.test(tag)) score = 6;
      if (score > bestScore) { bestScore = score; best = val; }
    }
    return best;
  }

  // URL-encoded form: a=1&code=123456&phone=...
  // 注意：HTML 文档里也含 `="..."`，但绝不应该走这个分支
  function _extractFromUrlEncoded(body) {
    if (/<\s*[a-zA-Z!\/]/.test(body)) return '';
    if (!/^[\w%+.\-]+=[^&\s]*(?:&[\w%+.\-]+=[^&\s]*)*$/.test(body.trim())) return '';
    let params;
    try { params = new URLSearchParams(body); } catch (e) { return ''; }
    let bestScore = 0;
    let best = '';
    for (const [k, v] of params.entries()) {
      if (!_isPlainCode(v)) continue;
      const normKey = _normalizeKey(k);
      let score = 0;
      if (_DIRECT_CODE_KEYS.has(normKey)) score = 10;
      else if (/code$/.test(normKey)) score = 8;
      else if (/code/.test(normKey)) score = 6;
      if (score > bestScore) { bestScore = score; best = String(v).trim(); }
    }
    return best;
  }

  function extractVerificationCode(rawBody) {
    let body = (rawBody || '').trim();
    if (!body) return '';

    // 去掉 BOM 和可能的 JSONP 包装
    if (body.charCodeAt(0) === 0xFEFF) body = body.slice(1);
    const unwrapped = _stripJsonpWrapper(body);

    // 1) 直接是 4-8 位数字字符串
    if (_isPlainCode(unwrapped)) return unwrapped.trim();

    // 2) JSON（含数组、JSONP、嵌套对象）
    try {
      const json = JSON.parse(unwrapped);
      const fromJson = _extractFromJsonValue(json);
      if (fromJson) return fromJson;
      // JSON 对象里没有结构化的验证码 → 把 JSON 字符串化后做文本扫描
      const stringified = typeof json === 'string' ? json : JSON.stringify(json);
      const fromText = _pickBestTextCode(stringified);
      if (fromText) return fromText;
    } catch (e) {
      // 不是合法 JSON，继续走其它格式
    }

    // 3) XML / HTML 标签
    if (/<\s*[a-zA-Z]/.test(body)) {
      const fromXml = _extractFromXml(body);
      if (fromXml) return fromXml;
      // 富文本邮件 / 网页响应：先剥 HTML 再走文本扫码
      const stripped = _stripHtml(body);
      const fromHtmlText = _pickBestTextCode(stripped);
      if (fromHtmlText) return fromHtmlText;
    }

    // 4) URL-encoded
    const fromUrl = _extractFromUrlEncoded(body);
    if (fromUrl) return fromUrl;

    // 5) 纯文本兜底
    return _pickBestTextCode(body);
  }

  function pickVerificationCode(verificationState) {
    const state = verificationState || {};
    const preferredSource = state.preferredSource || 'auto';
    const emailState = state.sources?.email || {};

    if (preferredSource === 'email' && emailState.code) {
      return { code: emailState.code, source: 'email' };
    }
    if (emailState.code) {
      return { code: emailState.code, source: 'email' };
    }
    return { code: '', source: '' };
  }

  function buildFillData(data, verificationState) {
    const expDateNormalized = normalizeExpDate(data?.expDate || '');
    const exp = expDateNormalized.split('/');
    const expMonth = exp[0] || '';
    const expYearRaw = exp[1] || '';
    const expYear = expYearRaw.length > 2 ? expYearRaw.slice(-2) : expYearRaw;
    const expYearFull = expYearRaw.length === 2 ? '20' + expYearRaw : expYearRaw;
    const pickedCode = pickVerificationCode(verificationState);
    const emailCode = verificationState?.sources?.email?.code || '';
    const age = data?.age ? String(data.age) : String(generateRandomAge());
    const birthday = data?.birthday || generateRandomBirthday();
    return {
      email: data?.email || '',
      password: data?.password || '',
      cardNumber: data?.cardNumber || '',
      expDate: expDateNormalized,
      expDateCompact: expMonth && expYear ? `${expMonth}${expYear}` : '',
      expDateLong: expMonth && expYearFull ? `${expMonth}/${expYearFull}` : '',
      expDateLongCompact: expMonth && expYearFull ? `${expMonth}${expYearFull}` : '',
      expMonth,
      expYear,
      expYearFull,
      cvv: data?.cvv || '',
      phone: data?.phoneClean || '',
      phoneFull: data?.smsPhone || data?.phone || '',
      name: data?.name || '',
      firstName: data?.firstName || '',
      lastName: data?.lastName || '',
      age,
      birthday: birthday.slash,
      birthdayDash: birthday.dash,
      birthdayCompact: birthday.compact,
      birthdayUk: birthday.uk || `${birthday.day}/${birthday.month}/${birthday.year}`,
      birthdayYear: birthday.year,
      birthdayMonth: birthday.month,
      birthdayDay: birthday.day,
      street: data?.addressLine || data?.street || '',
      address: data?.addressLine || data?.address || '',
      city: data?.city || '',
      state: data?.state || '',
      zip: data?.zip || '',
      code: pickedCode.code || '',
      codeSource: pickedCode.source || '',
      emailCode,
      addressCountry: data?.addressCountry || 'us',
      cardCountry: data?.cardCountry || 'us',
      // 日本：汉字本名 + 假名读音 + 汉字地址
      firstNameKana: data?.firstNameKana || '',
      lastNameKana: data?.lastNameKana || '',
      nameKana: data?.nameKana || '',
      firstNameHira: data?.firstNameHira || '',
      lastNameHira: data?.lastNameHira || '',
      nameHira: data?.nameHira || '',
      prefecture: data?.prefecture || data?.state || '',
      town: data?.town || '',
      birthdayJp: birthday.jp || '',
      // 巴西：CPF + 街区 + 巴西格式生日
      cpf: data?.cpf || '',
      neighborhood: data?.neighborhood || '',
      birthdayBr: birthday.br || '',
      houseNumber: data?.houseNumber || data?.number || '',
    };
  }

  function parseCSVLine(line) {
    const result = [];
    let current = '';
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQuotes) {
        if (c === '"' && line[i + 1] === '"') { current += '"'; i++; }
        else if (c === '"') { inQuotes = false; }
        else { current += c; }
      } else {
        if (c === '"') { inQuotes = true; }
        else if (c === ',') { result.push(current.trim()); current = ''; }
        else { current += c; }
      }
    }
    result.push(current.trim());
    return result;
  }

  function buildRawFromCsvText(text) {
    const lines = text.split(/\r?\n/).map(l => l.trim()).filter(l => l);
    if (lines.length < 2) return [];

    const headers = lines[0].split(',').map(h => h.trim().toLowerCase());
    const fieldMap = {
      '卡号': 'cardNumber', 'card': 'cardNumber', 'card_number': 'cardNumber', 'cardnumber': 'cardNumber',
      '日期': 'expDate', 'exp': 'expDate', 'expiry': 'expDate', 'exp_date': 'expDate', 'expdate': 'expDate',
      'cvv': 'cvv', 'cvc': 'cvv', 'csc': 'cvv',
      '姓名': 'name', 'name': 'name', 'fullname': 'name', 'full_name': 'name',
      '地址': 'address', 'address': 'address',
      '手机号': 'phone', 'phone': 'phone', 'mobile': 'phone', 'tel': 'phone',
      '接码api': 'smsApi', 'sms_api': 'smsApi', 'smsapi': 'smsApi', 'api': 'smsApi',
      '接码号码': 'smsCombo', 'sms_combo': 'smsCombo',
    };
    const labelNames = {
      'cardNumber': '卡号', 'expDate': '日期', 'cvv': 'CVV',
      'name': '姓名', 'address': '地址', 'phone': '手机号',
      'smsApi': '接码API', 'smsCombo': '',
    };

    const records = [];
    for (let rowIndex = 1; rowIndex < lines.length; rowIndex++) {
      const values = parseCSVLine(lines[rowIndex]);
      const raw = [];
      for (let i = 0; i < headers.length; i++) {
        const mapped = fieldMap[headers[i]];
        if (!mapped || !values[i]) continue;
        if (mapped === 'smsCombo') {
          raw.push(values[i]);
          continue;
        }
        const label = labelNames[mapped];
        if (label) {
          raw.push(label);
          raw.push(values[i]);
        }
      }
      if (raw.length) records.push(raw.join('\n'));
    }
    return records;
  }

  // ============================================================
  //  TOTP（RFC 6238）：基于 Base32 密钥生成 6 位 30 秒动态码
  // ============================================================

  function _base32Decode(input) {
    const cleaned = (input || '').toUpperCase().replace(/[\s-]/g, '').replace(/=+$/, '');
    if (!cleaned) throw new Error('密钥为空');
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    const bytes = [];
    let bits = 0;
    let value = 0;
    for (const ch of cleaned) {
      const idx = alphabet.indexOf(ch);
      if (idx === -1) throw new Error('密钥包含非 Base32 字符: ' + ch);
      value = (value << 5) | idx;
      bits += 5;
      if (bits >= 8) {
        bits -= 8;
        bytes.push((value >>> bits) & 0xff);
      }
    }
    return new Uint8Array(bytes);
  }

  async function generateTotp(secret, options) {
    const opts = options || {};
    const period = opts.period || 30;
    const digits = opts.digits || 6;
    const algorithm = (opts.algorithm || 'SHA-1').toUpperCase();
    const epoch = typeof opts.timestamp === 'number' ? opts.timestamp : Date.now();
    const counter = Math.floor(epoch / 1000 / period);

    const keyBytes = _base32Decode(secret);
    const counterBytes = new Uint8Array(8);
    let c = counter;
    for (let i = 7; i >= 0; i--) {
      counterBytes[i] = c & 0xff;
      c = Math.floor(c / 256);
    }

    const cryptoKey = await crypto.subtle.importKey(
      'raw',
      keyBytes,
      { name: 'HMAC', hash: { name: algorithm } },
      false,
      ['sign']
    );
    const signature = new Uint8Array(await crypto.subtle.sign('HMAC', cryptoKey, counterBytes));
    const offset = signature[signature.length - 1] & 0x0f;
    const binary =
      ((signature[offset] & 0x7f) << 24) |
      ((signature[offset + 1] & 0xff) << 16) |
      ((signature[offset + 2] & 0xff) << 8) |
      (signature[offset + 3] & 0xff);
    const code = binary % Math.pow(10, digits);
    return String(code).padStart(digits, '0');
  }

  function getTotpRemainingSeconds(period, timestamp) {
    const p = period || 30;
    const ts = typeof timestamp === 'number' ? timestamp : Date.now();
    return p - Math.floor(ts / 1000) % p;
  }

  function parseOtpAuthUri(uri) {
    if (!uri || typeof uri !== 'string') return null;
    const trimmed = uri.trim();
    if (!/^otpauth:\/\/totp\//i.test(trimmed)) return null;
    try {
      const url = new URL(trimmed);
      const secret = url.searchParams.get('secret') || '';
      const issuer = url.searchParams.get('issuer') || '';
      const labelRaw = decodeURIComponent(url.pathname.replace(/^\/+/, '').replace(/^totp\//i, ''));
      let label = labelRaw;
      if (issuer && labelRaw.startsWith(issuer + ':')) label = labelRaw.slice(issuer.length + 1);
      return {
        secret: secret.trim(),
        label: (label || labelRaw || '').trim(),
        issuer: issuer.trim(),
      };
    } catch (e) {
      return null;
    }
  }

  function normalizeTotpSecret(input) {
    return (input || '').toUpperCase().replace(/[\s-]/g, '').replace(/=+$/, '');
  }

  return {
    generateRandomPrefix,
    generateRandomQQEmail,
    generateRandomAddress,
    generateRandomName,
    generateJpIdentity,
    generateBrIdentity,
    generateRandomCardNumber,
    generateRandomBirthday,
    buildFillData,
    extractVerificationCode,
    pickVerificationCode,
    parseCSVLine,
    buildRawFromCsvText,
    generateTotp,
    getTotpRemainingSeconds,
    parseOtpAuthUri,
    normalizeTotpSecret,
  };
})();

if (typeof globalThis !== 'undefined') {
  globalThis.SmartFormFillerCore = SmartFormFillerCore;
}

if (typeof window !== "undefined") window.SmartFormFillerCore = SmartFormFillerCore;
