import json, os, random
BASE = "https://raw.githubusercontent.com/worldrecordbit/ginnan-assets/main/memes/"
memes = sorted(os.listdir("memes"))
random.Random(42).shuffle(memes)

caps = [
"the lore is simple. same mom as doge. that's it. that's the coin",
"kabosu walked so ginnan could run 💛",
"good morning to everyone who understands what a real cat brother is",
"doge changed the internet. his brother watched from the couch. now it's his turn",
"you can fake a lot of things in crypto. you cannot fake being raised by kabosumama",
"some coins have roadmaps. we have a mom in chiba japan",
"15 years old and just getting started",
"such brother. very lore. wow",
"the family that memes together moons together",
"quietly one of the only coins with a real story behind it",
"imagine explaining to your grandkids that you faded doge's actual brother",
"woke up. checked on ginnan. all is well. going back to bed",
"cats built the internet. dogs built crypto. ginnan is both worlds in one tux",
"there is no team. there is no vc. there is a cat and his people",
"do only good everyday. ginnan edition",
"the moon is not a destination. it's a family tradition",
"this cat has seen every doge pump from the same living room. he knows",
"real ones know the bio. kabosu the shiba with 3 cats",
"a tuxedo is not a costume when you're born into royalty",
"if you know you know. if you don't, welcome",
"gm to the doge family and everyone in it 🐱💛",
"we don't chase narratives. we were born into one",
"one household in chiba japan produced more culture than most countries",
"the chart is temporary. the lore is forever",
"tell me one coin with a better origin story. i'll wait",
"doge's little brother wears a tux and takes zero days off",
"the quietest cat in the loudest market",
"somewhere in chiba a mom is proud of both her sons",
"you don't find coins like this. they find you",
"hold like the family is watching. because she is",
"the internet gave dogs their decade. the cat is just warming up",
"no promises. no hype. just a cat with the best big brother in history",
"study the bloodline before you fade it",
"every morning ginnan wakes up in the same house doge did. think about that",
"wholesome on the outside. diamond paws on the inside",
"such tux. very inevitable. wow",
"the meme economy runs on family and we are the family",
"good night to everyone holding. the cat sleeps well and so should you",
"first they ignore the cat. then they meme the cat. then they buy the cat",
"generational memes require generations. we have one",
"a coin for people who read the bio before they buy",
"his brother's face is on billboards. his mom runs a blog. he just vibes",
"the doge bloodline continues whether you're ready or not",
"chiba japan. center of the meme universe. always has been",
"you had doge in 2013 or you have ginnan now. history rhymes",
"the tux stays on during the dips",
"cats and dogs living together. mass appreciation",
"the most patient cat in crypto",
"his first 15 years were practice",
"anyone can make a cat coin. nobody else can make this cat coin",
"same couch. same mom. same destiny",
"gm. the cat is awake and the candles are gold",
"we're not early. we're family",
"a bloodline you can verify and a community you can't fake",
"the calm before the cat",
"legend has it he learned to hodl watching his brother",
"raised by the same hands that raised a legend",
"some things you inherit. meme greatness is one of them",
"the internet never forgot doge. it's about to remember his brother",
"one cat. one tux. one ticker",
"every family has a quiet one. ours wears a tuxedo",
"the blueprint was drawn in 2013. we're just finishing the house",
"if kabosu is the past and doge is the present, you already know the future",
"be honest. you've never seen lore this clean",
"gm from the doge household 💛",
"a memecoin with a birth certificate",
"the older brother made history. the younger one studied it",
"nothing in crypto is guaranteed except this cat's pedigree",
"you can't rug a bloodline",
"his mom has been posting him since before your favorite coin existed",
"culture compounds. so does family",
"the cat that grew up next to greatness",
"good morning to the realest family in crypto",
"first rule of the household: do only good everyday",
"he watched the doge era from the front row. now he takes the stage",
"the market sleeps. the lore doesn't",
"cats have nine lives. this one only needs one run",
"the least artificial thing on this app is a 15 year old cat from chiba",
"his brother is a global icon and he still sits on the same windowsill",
"we didn't pick the cat. history did",
"such family. very destiny. much gold. wow",
"the wholesome side of the casino",
"one day this will all be a documentary and you'll wish you were early",
"the quiet ones always surprise you",
"a coin backed by nothing except the greatest meme family alive",
"his portfolio is a food bowl and a windowsill and he's still winning",
"gm gm. tux on. candles gold",
"doge showed the world what a meme can do. his brother took notes for a decade",
"they don't make lore like this anymore. they literally can't",
"weekend plans: exist elegantly and hold",
"a cat this composed has never missed",
"you're not buying a token. you're joining a household",
"the bloodline doesn't need a bull market. but it would enjoy one",
"somewhere between wholesome and inevitable",
"the last true family business in crypto",
"his brother has a statue. he wants a candle. give the cat his candle",
"end the week like ginnan. calm, gold, unbothered"
]

slots = ["13:15","15:40","17:05","19:30","21:45","23:20","01:50"]  # UTC (9:15am-9:50pm ET)
days = ["2026-07-%02d"%d for d in range(30,32)] + ["2026-08-%02d"%d for d in range(1,13)]
posts, mi, ci = [], 0, 0
hf_slots = {(0,2):"HF_VIDEO_1",(0,5):"HF_IMAGE_A",(1,3):"HF_VIDEO_2",(2,2):"HF_IMAGE_B",(3,4):"HF_IMAGE_C",(5,2):"HF_VIDEO_1",(6,3):"HF_IMAGE_A",(8,2):"HF_VIDEO_2",(9,4):"HF_IMAGE_B",(11,2):"HF_IMAGE_C",(12,3):"HF_VIDEO_1",(13,2):"HF_VIDEO_2"}
for di, day in enumerate(days):
    for si, slot in enumerate(slots):
        # slot 23:20/01:50 UTC belong to same ET day; 01:50 rolls to next UTC day
        d = day
        if slot == "01:50":
            import datetime
            d = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
        media = hf_slots.get((di,si))
        if media is None:
            media = BASE + memes[mi % len(memes)]; mi += 1
        posts.append({"n": len(posts)+1, "date_utc": f"{d}T{slot}:00.000Z", "media": media, "caption": caps[ci % len(caps)]}); ci += 1

json.dump({"account":"TBD","posts":posts}, open("content/calendar.json","w"), indent=1) if os.path.isdir("content") else (os.mkdir("content"), json.dump({"account":"TBD","posts":posts}, open("content/calendar.json","w"), indent=1))
print(len(posts), "posts,", mi, "meme slots,", len(hf_slots), "higgsfield slots")
