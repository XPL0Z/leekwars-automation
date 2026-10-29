/**
 * Script pour LEVEL 35 (poireau méné, fermier Xploz)
 * Nouveautés : CHIP_FLAME (niv. 29) et CHIP_SHIELD (niv. 35)
 * 
 * Win rate : à mesurer (pas encore de vrais combats avec cet équipement)
 * 
 * Arme a équiper : WEAPON_PISTOL + WEAPON_MACHINE_GUN
 * Puces à équiper : CHIP_FLASH + CHIP_FLAME + CHIP_SHIELD +
 *                   CHIP_PROTEIN + CHIP_MOTIVATION + CHIP_CURE
 * 
 **/

// =============================================================
//  IA Poireau — LeekScript 4
//  IA générique : s'adapte automatiquement aux armes et puces
//  équipées (dégâts, soins, boucliers/buffs).
//
//  Tour type :
//   1. Déplacement vers une case d'où l'on peut tirer, ou attente
//      hors de portée jusqu'au tour 25 (anti-nul)
//   2. Bonus de PT, puis plan optimal des PT du tour (force avant
//      l'attaque, armes, puces, soins, boucliers)
//   3. PT restants : attaque gloutonne puis buffs
//   4. Fuite / mise à couvert avec les PM restants
// =============================================================

global INIT = false
global DAMAGE_CHIPS = []
global HEAL_CHIPS = []
global BUFF_CHIPS = []

// Anti-nul : on n'attend plus hors de portée à partir de ce tour
// (les combats s'arrêtent au tour 64)
global MAX_WAIT_TURN = 25

// Plan de tour : une action = [objet, estArme, coût, dégâts, force,
// bouclier, soin, utilisations max, priorité d'exécution]
global A_ITEM = 0
global A_WEAPON = 1
global A_COST = 2
global A_DAMAGE = 3
global A_STRENGTH = 4
global A_SHIELD = 5
global A_HEAL = 6
global A_USES = 7
global A_PRIO = 8
global PLAN_ACTIONS = []
global PLAN_CURRENT = []
global PLAN_BEST = []
global PLAN_BEST_VALUE = 0
global PLAN_BEST_TP = 0
global PLAN_ENEMY_LIFE = 0
global PLAN_MISSING_LIFE = 0

// ---------- Outils ----------

function hasEffect(effects, types) {
	for (var e in effects) {
		if (inArray(types, e[0])) return true
	}
	return false
}

// Dégâts moyens d'une liste d'effets (le poison compte sur sa durée)
function averageDamage(effects) {
	var total = 0
	for (var e in effects) {
		if (e[0] == EFFECT_DAMAGE) {
			total += (e[1] + e[2]) / 2
		} else if (e[0] == EFFECT_POISON) {
			total += (e[1] + e[2]) / 2 * max(1, e[3])
		}
	}
	return total
}

function initChips() {
	for (var chip in getChips()) {
		var fx = getChipEffects(chip)
		if (hasEffect(fx, [EFFECT_DAMAGE, EFFECT_POISON])) {
			push(DAMAGE_CHIPS, chip)
		} else if (hasEffect(fx, [EFFECT_HEAL])) {
			push(HEAL_CHIPS, chip)
		} else if (hasEffect(fx, [EFFECT_ABSOLUTE_SHIELD, EFFECT_RELATIVE_SHIELD,
				EFFECT_BUFF_STRENGTH, EFFECT_BUFF_AGILITY, EFFECT_BUFF_RESISTANCE,
				EFFECT_BUFF_WISDOM, EFFECT_BUFF_MP, EFFECT_BUFF_TP])) {
			push(BUFF_CHIPS, chip)
		}
	}
	INIT = true
}

function canCastChip(chip) {
	return getCooldown(chip) == 0 && getChipCost(chip) <= getTP()
}

// Arme avec le meilleur ratio dégâts / PT
function bestWeapon() {
	var best = null
	var bestScore = -1
	for (var w in getWeapons()) {
		var s = averageDamage(getWeaponEffects(w)) / max(1, getWeaponCost(w))
		if (s > bestScore) {
			bestScore = s
			best = w
		}
	}
	return best
}

// Puce de dégâts avec la meilleure portée (pour se placer sans arme)
function bestDamageChip() {
	var best = null
	var bestDamage = -1
	for (var c in DAMAGE_CHIPS) {
		var d = averageDamage(getChipEffects(c))
		if (d > bestDamage) {
			bestDamage = d
			best = c
		}
	}
	return best
}

// ---------- Phases du tour ----------

// Cases libres atteignables avec les PM restants (case actuelle incluse)
function reachableCells() {
	var mp = getMP()
	var myCell = getCell()
	var x = getCellX(myCell)
	var y = getCellY(myCell)
	var cells = [myCell]
	for (var dx = -mp; dx <= mp; dx++) {
		for (var dy = -mp; dy <= mp; dy++) {
			if (abs(dx) + abs(dy) > mp) continue
			var c = getCellFromXY(x + dx, y + dy)
			if (c == null || c == myCell || !isEmptyCell(c)) continue
			var length = getPathLength(myCell, c)
			if (length != null && length <= mp) push(cells, c)
		}
	}
	return cells
}

// Distance à laquelle l'ennemi peut nous toucher à son prochain tour
// (approximation : PM + plus grande portée, sans tenir compte des obstacles)
function enemyThreatRange(enemy) {
	var range = 0
	for (var w in getWeapons(enemy)) {
		range = max(range, getWeaponMaxRange(w))
	}
	for (var c in getChips(enemy)) {
		if (hasEffect(getChipEffects(c), [EFFECT_DAMAGE, EFFECT_POISON])) {
			range = max(range, getChipMaxRange(c))
		}
	}
	return getMP(enemy) + range
}

// Si on ne peut pas tirer ce tour-ci, on attend juste hors de portée
// de l'ennemi, au plus près de lui, pour tirer en premier au tour suivant.
// Renvoie false si aucune case sûre n'existe (l'ennemi porte trop loin).
function waitOutOfRange(enemy) {
	var enemyCell = getCell(enemy)
	var threat = enemyThreatRange(enemy)
	var best = null
	var bestDistance = 999999
	for (var c in reachableCells()) {
		var d = getCellDistance(c, enemyCell)
		if (d > threat && d < bestDistance) {
			bestDistance = d
			best = c
		}
	}
	if (best == null) return false
	if (best != getCell()) moveTowardCell(best)
	return true
}

function moveToAttack(enemy) {
	var myCell = getCell()
	// Même si l'ennemi recule, on le suit au bord de sa portée plutôt
	// que d'y entrer : sinon un ennemi plus mobile tire en premier
	var mayWait = getTurn() < MAX_WAIT_TURN

	var weapon = bestWeapon()
	var cells = []
	if (weapon != null) {
		cells = getCellsToUseWeapon(weapon, enemy)
	} else {
		var chip = bestDamageChip()
		if (chip != null) cells = getCellsToUseChip(chip, enemy)
	}
	if (cells == null) cells = []
	if (inArray(cells, myCell)) return null

	var best = null
	var bestLength = 999999
	for (var c in cells) {
		var length = getPathLength(myCell, c)
		if (length != null && length < bestLength) {
			bestLength = length
			best = c
		}
	}
	if (best != null && bestLength <= getMP()) {
		moveTowardCell(best)
	} else if (mayWait && waitOutOfRange(enemy)) {
		return true
	} else if (best != null) {
		moveTowardCell(best)
	} else {
		moveToward(enemy)
	}
}

// ---------- Plan de tour ----------

function averageEffect(effects, types) {
	var total = 0
	for (var e in effects) {
		if (inArray(types, e[0])) total += (e[1] + e[2]) / 2
	}
	return total
}

// Bonus de PT (motivation...) : lancés en premier pour servir ce tour-ci
function castTPBuffs() {
	var me = getEntity()
	for (var chip in BUFF_CHIPS) {
		if (hasEffect(getChipEffects(chip), [EFFECT_BUFF_TP]) && canCastChip(chip)) {
			useChip(chip, me)
		}
	}
}

// Toutes les actions utiles ce tour-ci, depuis la case actuelle
function buildActions(enemy, waiting) {
	var actions = []
	var canHit = false
	// Inutile de se protéger si l'ennemi ne peut pas nous toucher au prochain tour
	var threatened = getCellDistance(getCell(), getCell(enemy)) <= enemyThreatRange(enemy)
	for (var w in getWeapons()) {
		if (!canUseWeapon(w, enemy)) continue
		var prio = getWeapon() == w ? 1 : 2
		push(actions, [w, true, getWeaponCost(w), averageDamage(getWeaponEffects(w)), 0, 0, 0, 3, prio])
		canHit = true
	}
	for (var c in getChips()) {
		if (getCooldown(c) > 0) continue
		var fx = getChipEffects(c)
		var uses = getChipCooldown(c) > 0 ? 1 : 3
		var damage = averageDamage(fx)
		if (damage > 0) {
			if (canUseChip(c, enemy)) {
				push(actions, [c, false, getChipCost(c), damage, 0, 0, 0, uses, 3])
				canHit = true
			}
			continue
		}
		var heal = averageEffect(fx, [EFFECT_HEAL]) * (1 + getWisdom() / 100)
		if (heal > 0) {
			push(actions, [c, false, getChipCost(c), 0, 0, 0, heal, 1, 4])
			continue
		}
		// Un bouclier absolu absorbe sa valeur sur ~3 coups par tour
		var shield = averageEffect(fx, [EFFECT_ABSOLUTE_SHIELD]) * 3
			+ averageEffect(fx, [EFFECT_RELATIVE_SHIELD]) * 1.5
		if (shield > 0 && !waiting && threatened) {
			push(actions, [c, false, getChipCost(c), 0, 0, shield, 0, 1, 5])
		}
	}
	// Les bonus de force ne valent le coup que si on tape ce tour-ci
	if (canHit) {
		for (var c in BUFF_CHIPS) {
			if (getCooldown(c) > 0) continue
			var str = averageEffect(getChipEffects(c), [EFFECT_BUFF_STRENGTH])
			if (str > 0) push(actions, [c, false, getChipCost(c), 0, str, 0, 0, 1, 0])
		}
	}
	return actions
}

function planValue() {
	var strength = getStrength()
	var damage = 0
	var heal = 0
	var shield = 0
	for (var i in PLAN_CURRENT) {
		strength += PLAN_ACTIONS[i][A_STRENGTH]
	}
	for (var i in PLAN_CURRENT) {
		var a = PLAN_ACTIONS[i]
		damage += a[A_DAMAGE] * (1 + strength / 100)
		heal += a[A_HEAL]
		shield += a[A_SHIELD]
	}
	return min(damage, PLAN_ENEMY_LIFE) + min(heal, PLAN_MISSING_LIFE) + shield
}

function countUses(index) {
	var n = 0
	for (var i in PLAN_CURRENT) {
		if (i == index) n++
	}
	return n
}

// Explore toutes les combinaisons d'actions (sans ordre, avec répétition)
// qui tiennent dans les PT ; changer d'arme coûte 1 PT de plus
function searchPlan(start, tpLeft) {
	// À valeur égale on garde le plan qui laisse le plus de PT : évite
	// les actions inutiles (soin à pleine vie...)
	var value = planValue()
	if (value > PLAN_BEST_VALUE || (value == PLAN_BEST_VALUE && tpLeft > PLAN_BEST_TP)) {
		PLAN_BEST_VALUE = value
		PLAN_BEST_TP = tpLeft
		PLAN_BEST = clone(PLAN_CURRENT)
	}
	for (var i = start; i < count(PLAN_ACTIONS); i++) {
		var a = PLAN_ACTIONS[i]
		var cost = a[A_COST]
		if (a[A_WEAPON] && getWeapon() != a[A_ITEM] && countUses(i) == 0) cost++
		if (cost > tpLeft || countUses(i) >= a[A_USES]) continue
		push(PLAN_CURRENT, i)
		searchPlan(i, tpLeft - cost)
		pop(PLAN_CURRENT)
	}
}

// Calcule puis joue le meilleur plan : force, armes, puces, soins, boucliers
function playTurn(enemy, waiting) {
	PLAN_ACTIONS = buildActions(enemy, waiting)
	PLAN_CURRENT = []
	PLAN_BEST = []
	PLAN_BEST_VALUE = 0
	PLAN_BEST_TP = 0
	PLAN_ENEMY_LIFE = getLife(enemy) + getAbsoluteShield(enemy)
	PLAN_MISSING_LIFE = getTotalLife() - getLife()
	searchPlan(0, getTP())

	var me = getEntity()
	for (var prio = 0; prio <= 5; prio++) {
		for (var i in PLAN_BEST) {
			var a = PLAN_ACTIONS[i]
			if (a[A_PRIO] != prio || !isAlive(enemy)) continue
			if (a[A_WEAPON]) {
				if (getWeapon() != a[A_ITEM]) setWeapon(a[A_ITEM])
				useWeapon(enemy)
			} else if (a[A_DAMAGE] > 0) {
				useChip(a[A_ITEM], enemy)
			} else {
				useChip(a[A_ITEM], me)
			}
		}
	}
}

// Utilise à chaque fois l'action qui fait le plus de dégâts par PT
function attack(enemy) {
	var failed = []
	while (getTP() > 0 && isAlive(enemy)) {
		var bestScore = 0
		var bestAction = null
		var bestIsWeapon = false

		for (var w in getWeapons()) {
			if (inArray(failed, w)) continue
			var cost = getWeaponCost(w) + (getWeapon() == w ? 0 : 1)
			if (cost <= getTP() && canUseWeapon(w, enemy)) {
				var s = averageDamage(getWeaponEffects(w)) / cost
				if (s > bestScore) {
					bestScore = s
					bestAction = w
					bestIsWeapon = true
				}
			}
		}
		for (var c in DAMAGE_CHIPS) {
			if (inArray(failed, c)) continue
			if (canCastChip(c) && canUseChip(c, enemy)) {
				var s = averageDamage(getChipEffects(c)) / max(1, getChipCost(c))
				if (s > bestScore) {
					bestScore = s
					bestAction = c
					bestIsWeapon = false
				}
			}
		}

		if (bestAction == null) break

		var result
		if (bestIsWeapon) {
			if (getWeapon() != bestAction) setWeapon(bestAction)
			result = useWeapon(enemy)
		} else {
			result = useChip(bestAction, enemy)
		}
		if (result != USE_SUCCESS && result != USE_CRITICAL) {
			push(failed, bestAction)
		}
	}
}

// PT restants : buffs durables (la force et les PT sont gérés avant l'attaque)
function buffSelf() {
	var me = getEntity()
	for (var chip in BUFF_CHIPS) {
		if (hasEffect(getChipEffects(chip), [EFFECT_BUFF_STRENGTH, EFFECT_BUFF_TP])) continue
		if (canCastChip(chip)) useChip(chip, me)
	}
}

// Cherche une case accessible hors de vue de l'ennemi, sinon la plus éloignée
function hide(enemy) {
	if (getMP() <= 0) return null
	var me = getEntity()
	var myCell = getCell()
	var enemyCell = getCell(enemy)

	var best = myCell
	var bestScore = -1
	for (var c in reachableCells()) {
		var score = (lineOfSight(c, enemyCell, [me, enemy]) ? 0 : 1000)
			+ getCellDistance(c, enemyCell)
		if (score > bestScore) {
			bestScore = score
			best = c
		}
	}
	if (best != myCell) moveTowardCell(best)
}

// ---------- Tour ----------

// Poireau ennemi le plus proche : on ignore les coffres (énormes PV,
// ils ripostent quand on les frappe) et autres entités non poireaux
function getTarget() {
	var best = null
	var bestDistance = 999999
	for (var e in getAliveEnemies()) {
		if (getType(e) != ENTITY_LEEK) continue
		var d = getCellDistance(getCell(), getCell(e))
		if (d < bestDistance) {
			bestDistance = d
			best = e
		}
	}
	return best
}

function main() {
	if (!INIT) initChips()

	var enemy = getTarget()
	if (enemy == null) return null

	var waiting = moveToAttack(enemy) == true
	if (!waiting) castTPBuffs()
	playTurn(enemy, waiting)
	attack(enemy)
	if (!waiting) buffSelf()
	if (!waiting) hide(enemy)
}

main()
